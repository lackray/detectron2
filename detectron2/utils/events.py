# Copyright (c) Facebook, Inc. and its affiliates.
import datetime
import enum
import json
import yaml
import logging
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Optional
from detectron2.structures.instances import Instances
import torch
from fvcore.common.history_buffer import HistoryBuffer
import wandb

from detectron2.utils.file_io import PathManager
from detectron2.config import CfgNode
from detectron2.data import build_detection_test_loader
from detectron2.data import MetadataCatalog


__all__ = [
    "get_event_storage",
    "JSONWriter",
    "TensorboardXWriter",
    "CommonMetricPrinter",
    "EventStorage",
]

_CURRENT_STORAGE_STACK = []


def get_event_storage():
    """
    Returns:
        The :class:`EventStorage` object that's currently being used.
        Throws an error if no :class:`EventStorage` is currently enabled.
    """
    assert len(
        _CURRENT_STORAGE_STACK
    ), "get_event_storage() has to be called inside a 'with EventStorage(...)' context!"
    return _CURRENT_STORAGE_STACK[-1]


class EventWriter:
    """
    Base class for writers that obtain events from :class:`EventStorage` and process them.
    """

    def write(self):
        raise NotImplementedError

    def close(self):
        pass


class JSONWriter(EventWriter):
    """
    Write scalars to a json file.

    It saves scalars as one json per line (instead of a big json) for easy parsing.

    Examples parsing such a json file:
    ::
        $ cat metrics.json | jq -s '.[0:2]'
        [
          {
            "data_time": 0.008433341979980469,
            "iteration": 19,
            "loss": 1.9228371381759644,
            "loss_box_reg": 0.050025828182697296,
            "loss_classifier": 0.5316952466964722,
            "loss_mask": 0.7236229181289673,
            "loss_rpn_box": 0.0856662318110466,
            "loss_rpn_cls": 0.48198649287223816,
            "lr": 0.007173333333333333,
            "time": 0.25401854515075684
          },
          {
            "data_time": 0.007216215133666992,
            "iteration": 39,
            "loss": 1.282649278640747,
            "loss_box_reg": 0.06222952902317047,
            "loss_classifier": 0.30682939291000366,
            "loss_mask": 0.6970193982124329,
            "loss_rpn_box": 0.038663312792778015,
            "loss_rpn_cls": 0.1471673548221588,
            "lr": 0.007706666666666667,
            "time": 0.2490077018737793
          }
        ]

        $ cat metrics.json | jq '.loss_mask'
        0.7126231789588928
        0.689423680305481
        0.6776131987571716
        ...

    """

    def __init__(self, json_file, window_size=20):
        """
        Args:
            json_file (str): path to the json file. New data will be appended if the file exists.
            window_size (int): the window size of median smoothing for the scalars whose
                `smoothing_hint` are True.
        """
        self._file_handle = PathManager.open(json_file, "a")
        self._window_size = window_size
        self._last_write = -1

    def write(self):
        storage = get_event_storage()
        to_save = defaultdict(dict)

        for k, (v, iter) in storage.latest_with_smoothing_hint(self._window_size).items():
            # keep scalars that have not been written
            if iter <= self._last_write:
                continue
            to_save[iter][k] = v
        if len(to_save):
            all_iters = sorted(to_save.keys())
            self._last_write = max(all_iters)

        for itr, scalars_per_iter in to_save.items():
            scalars_per_iter["iteration"] = itr
            self._file_handle.write(json.dumps(scalars_per_iter, sort_keys=True) + "\n")
        self._file_handle.flush()
        try:
            os.fsync(self._file_handle.fileno())
        except AttributeError:
            pass

    def close(self):
        self._file_handle.close()


class TensorboardXWriter(EventWriter):
    """
    Write all scalars to a tensorboard file.
    """

    def __init__(self, log_dir: str, window_size: int = 20, **kwargs):
        """
        Args:
            log_dir (str): the directory to save the output events
            window_size (int): the scalars will be median-smoothed by this window size

            kwargs: other arguments passed to `torch.utils.tensorboard.SummaryWriter(...)`
        """
        self._window_size = window_size
        from torch.utils.tensorboard import SummaryWriter

        self._writer = SummaryWriter(log_dir, **kwargs)
        self._last_write = -1

    def write(self):
        storage = get_event_storage()
        new_last_write = self._last_write
        for k, (v, iter) in storage.latest_with_smoothing_hint(self._window_size).items():
            if iter > self._last_write:
                self._writer.add_scalar(k, v, iter)
                new_last_write = max(new_last_write, iter)
        self._last_write = new_last_write

        # storage.put_{image,histogram} is only meant to be used by
        # tensorboard writer. So we access its internal fields directly from here.
        if len(storage._vis_data) >= 1:
            for img_name, img, step_num in storage._vis_data:
                self._writer.add_image(img_name, img, step_num)
            # Storage stores all image data and rely on this writer to clear them.
            # As a result it assumes only one writer will use its image data.
            # An alternative design is to let storage store limited recent
            # data (e.g. only the most recent image) that all writers can access.
            # In that case a writer may not see all image data if its period is long.
            storage.clear_images()

        if len(storage._histograms) >= 1:
            for params in storage._histograms:
                self._writer.add_histogram_raw(**params)
            storage.clear_histograms()

    def close(self):
        if hasattr(self, "_writer"):  # doesn't exist when the code fails at import
            self._writer.close()


class CommonMetricPrinter(EventWriter):
    """
    Print **common** metrics to the terminal, including
    iteration time, ETA, memory, all losses, and the learning rate.
    It also applies smoothing using a window of 20 elements.

    It's meant to print common metrics in common ways.
    To print something in more customized ways, please implement a similar printer by yourself.
    """

    def __init__(self, max_iter: Optional[int] = None, window_size: int = 20):
        """
        Args:
            max_iter: the maximum number of iterations to train.
                Used to compute ETA. If not given, ETA will not be printed.
            window_size (int): the losses will be median-smoothed by this window size
        """
        self.logger = logging.getLogger(__name__)
        self._max_iter = max_iter
        self._window_size = window_size
        self._last_write = None  # (step, time) of last call to write(). Used to compute ETA

    def _get_eta(self, storage) -> Optional[str]:
        if self._max_iter is None:
            return ""
        iteration = storage.iter
        try:
            eta_seconds = storage.history("time").median(1000) * (self._max_iter - iteration - 1)
            storage.put_scalar("eta_seconds", eta_seconds, smoothing_hint=False)
            return str(datetime.timedelta(seconds=int(eta_seconds)))
        except KeyError:
            # estimate eta on our own - more noisy
            eta_string = None
            if self._last_write is not None:
                estimate_iter_time = (time.perf_counter() - self._last_write[1]) / (
                        iteration - self._last_write[0]
                )
                eta_seconds = estimate_iter_time * (self._max_iter - iteration - 1)
                eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))
            self._last_write = (iteration, time.perf_counter())
            return eta_string

    def write(self):
        storage = get_event_storage()
        iteration = storage.iter
        if iteration == self._max_iter:
            # This hook only reports training progress (loss, ETA, etc) but not other data,
            # therefore do not write anything after training succeeds, even if this method
            # is called.
            return

        try:
            data_time = storage.history("data_time").avg(20)
        except KeyError:
            # they may not exist in the first few iterations (due to warmup)
            # or when SimpleTrainer is not used
            data_time = None
        try:
            iter_time = storage.history("time").global_avg()
        except KeyError:
            iter_time = None
        try:
            lr = "{:.5g}".format(storage.history("lr").latest())
        except KeyError:
            lr = "N/A"

        eta_string = self._get_eta(storage)

        if torch.cuda.is_available():
            max_mem_mb = torch.cuda.max_memory_allocated() / 1024.0 / 1024.0
        else:
            max_mem_mb = None

        # NOTE: max_mem is parsed by grep in "dev/parse_results.sh"
        self.logger.info(
            " {eta}iter: {iter}  {losses}  {time}{data_time}lr: {lr}  {memory}".format(
                eta=f"eta: {eta_string}  " if eta_string else "",
                iter=iteration,
                losses="  ".join(
                    [
                        "{}: {:.4g}".format(k, v.median(self._window_size))
                        for k, v in storage.histories().items()
                        if "loss" in k
                    ]
                ),
                time="time: {:.4f}  ".format(iter_time) if iter_time is not None else "",
                data_time="data_time: {:.4f}  ".format(data_time) if data_time is not None else "",
                lr=lr,
                memory="max_mem: {:.0f}M".format(max_mem_mb) if max_mem_mb is not None else "",
            )
        )


class EventStorage:
    """
    The user-facing class that provides metric storage functionalities.

    In the future we may add support for storing / logging other types of data if needed.
    """

    def __init__(self, start_iter=0):
        """
        Args:
            start_iter (int): the iteration number to start with
        """
        self._history = defaultdict(HistoryBuffer)
        self._smoothing_hints = {}
        self._latest_scalars = {}
        self._iter = start_iter
        self._current_prefix = ""
        self._vis_data = []
        self._histograms = []
        self._predictions = []

    def put_image(self, img_name, img_tensor):
        """
        Add an `img_tensor` associated with `img_name`, to be shown on
        tensorboard.

        Args:
            img_name (str): The name of the image to put into tensorboard.
            img_tensor (torch.Tensor or numpy.array): An `uint8` or `float`
                Tensor of shape `[channel, height, width]` where `channel` is
                3. The image format should be RGB. The elements in img_tensor
                can either have values in [0, 1] (float32) or [0, 255] (uint8).
                The `img_tensor` will be visualized in tensorboard.
        """
        self._vis_data.append((img_name, img_tensor, self._iter))
    
    def put_predictions(self, preds):
        """
        Add a list of predictions on test set

        Args:
            preds [List]: list containing latest predictions made on test set
        """
        self._predictions.extend(preds)


    def put_scalar(self, name, value, smoothing_hint=True):
        """
        Add a scalar `value` to the `HistoryBuffer` associated with `name`.

        Args:
            smoothing_hint (bool): a 'hint' on whether this scalar is noisy and should be
                smoothed when logged. The hint will be accessible through
                :meth:`EventStorage.smoothing_hints`.  A writer may ignore the hint
                and apply custom smoothing rule.

                It defaults to True because most scalars we save need to be smoothed to
                provide any useful signal.
        """
        name = self._current_prefix + name
        history = self._history[name]
        value = float(value)
        history.update(value, self._iter)
        self._latest_scalars[name] = (value, self._iter)

        existing_hint = self._smoothing_hints.get(name)
        if existing_hint is not None:
            assert (
                    existing_hint == smoothing_hint
            ), "Scalar {} was put with a different smoothing_hint!".format(name)
        else:
            self._smoothing_hints[name] = smoothing_hint

    def put_scalars(self, *, smoothing_hint=True, **kwargs):
        """
        Put multiple scalars from keyword arguments.

        Examples:

            storage.put_scalars(loss=my_loss, accuracy=my_accuracy, smoothing_hint=True)
        """
        for k, v in kwargs.items():
            self.put_scalar(k, v, smoothing_hint=smoothing_hint)

    def put_histogram(self, hist_name, hist_tensor, bins=1000):
        """
        Create a histogram from a tensor.

        Args:
            hist_name (str): The name of the histogram to put into tensorboard.
            hist_tensor (torch.Tensor): A Tensor of arbitrary shape to be converted
                into a histogram.
            bins (int): Number of histogram bins.
        """
        ht_min, ht_max = hist_tensor.min().item(), hist_tensor.max().item()

        # Create a histogram with PyTorch
        hist_counts = torch.histc(hist_tensor, bins=bins)
        hist_edges = torch.linspace(start=ht_min, end=ht_max, steps=bins + 1, dtype=torch.float32)

        # Parameter for the add_histogram_raw function of SummaryWriter
        hist_params = dict(
            tag=hist_name,
            min=ht_min,
            max=ht_max,
            num=len(hist_tensor),
            sum=float(hist_tensor.sum()),
            sum_squares=float(torch.sum(hist_tensor ** 2)),
            bucket_limits=hist_edges[1:].tolist(),
            bucket_counts=hist_counts.tolist(),
            global_step=self._iter,
        )
        self._histograms.append(hist_params)

    def history(self, name):
        """
        Returns:
            HistoryBuffer: the scalar history for name
        """
        ret = self._history.get(name, None)
        if ret is None:
            raise KeyError("No history metric available for {}!".format(name))
        return ret

    def histories(self):
        """
        Returns:
            dict[name -> HistoryBuffer]: the HistoryBuffer for all scalars
        """
        return self._history

    def latest(self):
        """
        Returns:
            dict[str -> (float, int)]: mapping from the name of each scalar to the most
                recent value and the iteration number its added.
        """
        return self._latest_scalars

    def latest_with_smoothing_hint(self, window_size=20):
        """
        Similar to :meth:`latest`, but the returned values
        are either the un-smoothed original latest value,
        or a median of the given window_size,
        depend on whether the smoothing_hint is True.

        This provides a default behavior that other writers can use.
        """
        result = {}
        for k, (v, itr) in self._latest_scalars.items():
            result[k] = (
                self._history[k].median(window_size) if self._smoothing_hints[k] else v,
                itr,
            )
        return result

    def smoothing_hints(self):
        """
        Returns:
            dict[name -> bool]: the user-provided hint on whether the scalar
                is noisy and needs smoothing.
        """
        return self._smoothing_hints

    def step(self):
        """
        User should either: (1) Call this function to increment storage.iter when needed. Or
        (2) Set `storage.iter` to the correct iteration number before each iteration.

        The storage will then be able to associate the new data with an iteration number.
        """
        self._iter += 1

    @property
    def iter(self):
        """
        Returns:
            int: The current iteration number. When used together with a trainer,
                this is ensured to be the same as trainer.iter.
        """
        return self._iter

    @iter.setter
    def iter(self, val):
        self._iter = int(val)

    @property
    def iteration(self):
        # for backward compatibility
        return self._iter

    def __enter__(self):
        _CURRENT_STORAGE_STACK.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        assert _CURRENT_STORAGE_STACK[-1] == self
        _CURRENT_STORAGE_STACK.pop()

    @contextmanager
    def name_scope(self, name):
        """
        Yields:
            A context within which all the events added to this storage
            will be prefixed by the name scope.
        """
        old_prefix = self._current_prefix
        self._current_prefix = name.rstrip("/") + "/"
        yield
        self._current_prefix = old_prefix

    def clear_images(self):
        """
        Delete all the stored images for visualization. This should be called
        after images are written to tensorboard.
        """
        self._vis_data = []

    def clear_predictions(self):
        """
        Delete all predictions from `predictions` list.
        """
        self._predictions = []

    def clear_histograms(self):
        """
        Delete all the stored histograms for visualization.
        This should be called after histograms are written to tensorboard.
        """
        self._histograms = []


class WandbWriter(EventWriter):
    """
    Write all scalars to a wandb tool.
    """

    def __init__(self, cfg: CfgNode, window_size: int = 20, **kwargs):
        """
        Args:
            cfg (CfgNode): the project level configuration object
            window_size (int): the scalars will be median-smoothed by this window size

            kwargs: other arguments passed to `wandb.init(...)`
        """
        self._window_size = window_size
        self._media = []
        self.cfg = cfg
        self.thing_class_names = []
        self.thing_index_to_class = []
        self.stuff_class_names = []
        self.stuff_index_to_class = []
        self._table_thing_classes = [] # List[wandb.Classes] objects needed for tables
        self._table_stuff_classes = [] 
        self._evalset_table = []
        self._map_table_row_file_name = [] # Used to dedupe images: table.get[row_num] by mapping tablle row to file name
        self._build_dataset_metadata()
        metadata = MetadataCatalog.get(self._dataset[0])
        if cfg is None:
            cfg = {}
            wandb_project = "detectron2"
        else:
            wandb_project = cfg.WANDB.PROJECT_NAME
            cfg = yaml.load(cfg.dump())
        self._run = wandb.init(
            project=wandb_project,
            config=cfg,
            **kwargs
        )
        self._run._label(repo="detectron2")
    
    def _build_dataset_metadata(self):
        '''
        Builds parsed metadata lists and mappings dicts to facilitate logging.
        Builds a list of metadata for each of the validation dataloaders. Expects the metadata to present for ech dataloader or
        combined as a list of dataloaders. If both are present, the former will be given preferance.
        Useful for writing labels and captions of predictions.
        
        E.g. -- Setting the metadata in either ways will works:

        DATASETS.TEST/TEST = ("val1", "val2",)
        # set the grobal properties for both datasets
        MetadataCatalog.get(("val1", "val2",)).property = ...
        
        # set the properties separately
        MetadataCatalog.get("val1").property = ...
        MetadataCatalog.get("val2").property = ...
        
        Builds 3 metadata objects for stuff and thing classes:
        1. self.thing_class_names/self.stuff_class_names -- List[List[str]] of category names
        2. self.thing_index_to_class/self.stuff_index_to_class-- List[Dict[int, str]]
        3. self.thing_class_names/self.stuff_class_names -- List[wandb.Classes] for logging images to wandb.Table
        
        '''
        self._dataset = self.cfg.DATASETS.TEST if self.cfg.DATASETS.TEST else self.cfg.DATASETS.TRAIN
        self._num_loaders = len(self._dataset)
        combined_loader_meta = MetadataCatalog.get(self._dataset)
        for dataset in self._dataset:
            # represent each un-initialized loader in list
            self._evalset_table.append(None)
            self._map_table_row_file_name.append({})
            metadata = MetadataCatalog.get(dataset)

            # Parse thing_classes
            self.thing_class_names.append([])
            self.thing_index_to_class.append({})               
            if hasattr(metadata, 'thing_classes'): # if user provides individual lists for each dataset
                self.thing_class_names[-1] = metadata.thing_classes
            elif hasattr(combined_loader_meta, 'thing_classes'): # if user provides combined list for all datasets
                self.thing_class_names[-1] = combined_loader_meta.thing_classes
                
            index_to_class = {}
            wandb_thing_classes = []
            # NOTE: The classs indeces starts from 1 instead of 0. Treat 0 as void, makes for easier vectorized operations
            for i, name in enumerate(self.thing_class_names[-1], 1):
                index_to_class[i] = name
                wandb_thing_classes.append({"id": i, "name": name})
            self.thing_index_to_class[-1] = (index_to_class)
            self._table_thing_classes.append(wandb.Classes(wandb_thing_classes))

            # Parse stuff_classes
            self.stuff_class_names.append([])
            self.stuff_index_to_class.append({})
            if hasattr(metadata, 'stuff_classes'):
                self.stuff_class_names[-1] = metadata.stuff_classes
            elif hasattr(combined_loader_meta, 'stuff_classes'):
                self.stuff_class_names[-1] = combined_loader_meta.stuff_classes
                
            index_to_class = {}
            wandb_stuff_classes = []
            for i, name in enumerate(self.stuff_class_names[-1]):
                index_to_class[i] = name
                wandb_stuff_classes.append({"id": i, "name": name})
            self.stuff_index_to_class[-1] = index_to_class
            self._table_stuff_classes.append(wandb.Classes(wandb_stuff_classes))

    def _plot_prediction(self, pred):
        """
        plot prediction on one image

        Args:
            img (str): Path to the image
            pred (Dict): Prediction for one image
        
        """
        loader_i = pred['loader_idx']
        file_name = pred['file_name']
        classes = self._table_thing_classes[loader_i]
        # Process Bounding box detections
        boxes = {}
        avg_conf_per_class = [0 for i in range(len(self.thing_class_names[loader_i]))]
        counts = {}
        if pred.get('boxes') is not None:
            boxes_data = []
            # only plot top 20 predictions. Preds are sorted by descending conf. scores.
            for i, box in enumerate(pred['boxes']):
                pred_class = int(pred['classes'][i])
                caption = f'{pred_class}' if not self.thing_class_names[loader_i] else self.thing_class_names[loader_i][pred_class]

                boxes_data.append({"position": {"minX": box[0], "minY": box[1], "maxX": box[2], "maxY": box[3]},
                                "class_id": pred_class + 1,
                                "box_caption": "%s %.3f" % (caption, pred['scores'][i]),
                                "scores": {"class_score":  pred['scores'][i]},
                                "domain": "pixel"
                                })

                avg_conf_per_class[pred_class] = avg_conf_per_class[pred_class] + pred['scores'][i]
                if pred_class in counts:
                    counts[pred_class] = counts[pred_class] + 1
                else:
                    counts[pred_class] = 1

            for pred_class in counts.keys():
                avg_conf_per_class[pred_class] = avg_conf_per_class[pred_class] / counts[pred_class]

            boxes = {"predictions": {"box_data": boxes_data, "class_labels": self.thing_index_to_class[loader_i]}}
        
        # Process instance segmentation detections
        masks = {}
        if pred.get('pred_masks') is not None:
            class_count = {}
            num_pred = min(15, len(pred['pred_masks'])) # Hardcoded to max 15 masks for better UI 
            for i in range(num_pred):
                pred_class = int(pred['classes'][i])
                if pred_class in class_count:
                    class_count[pred_class] = class_count[pred_class] + 1
                else:
                    class_count[pred_class] = 0

                # title format - class_count. E.g - person_0, person_1 ..
                mask_title = f'class {pred_class}' if not self.thing_class_names[loader_i] else self.thing_class_names[loader_i][pred_class]
                mask_title = f'{mask_title}_{class_count[pred_class]}'
                
                masks[mask_title] = {
                    "mask_data": pred['pred_masks'][i]*(pred_class+1),
                    "class_labels": {pred_class+1: mask_title}
                }
         
        # Process semantic segmentation predictions
        if pred.get("sem_mask") is not None:
            masks["semantic_mask"] = {
                "mask_data": pred["sem_mask"],
                "class_labels": self.stuff_index_to_class[loader_i]
            }
            classes = self._table_stuff_classes[loader_i]
            
        # TODO: Support panoptic segmentation and keypoint visualizations. If we cannot support the interactive version,
        # use Visualizer class to log static predictions.
        if pred.get("panoptic_mask") is not None:
            masks["panoptic_mask"] = {
                "mask_data": pred["panoptic_mask"],
                "class_labels": self.stuff_index_to_class[loader_i]
            }


        return (wandb.Image(file_name, boxes=boxes, masks=masks, classes=classes), avg_conf_per_class)

    def write(self):
        storage = get_event_storage()

        log_dict = {}
        tables = self._build_evalset_tables()
        table_row_idx = [0 for i in range(len(tables))]
        self._media = []
        if len(storage._predictions):
            # NOTE: there can be mutliple datasets used together like -('coco', 'voc')
            # we need to handle each dataset and corresponding table separately 
            for pred in storage._predictions:
                loader_i = pred['loader_idx']
                file_name = pred['file_name']
                pred_img, avg_bbox_conf = self._plot_prediction(pred)

                # hardcode media panel images upper limit to 8
                if len(self._media) < 8:
                    self._media.append(pred_img)
                if self._table_logging():
                    # Log images only once, then use their refernces to dedupe
                    if self._evalset_table[loader_i] is None:
                        tables[loader_i].add_data(file_name, pred_img, *avg_bbox_conf)
                        self._map_table_row_file_name[loader_i][file_name] = table_row_idx[loader_i]
                        table_row_idx[loader_i] = table_row_idx[loader_i] + 1
                    else:
                        row_idx = self._map_table_row_file_name[loader_i][file_name]
                        table_row = self._evalset_table[loader_i].data[row_idx]
                        tables[loader_i].add_data(
                            table_row[0],
                                wandb.Image(table_row[1], boxes=pred_img._boxes, masks=pred_img._masks),
                                *avg_bbox_conf
                                )

            if self._table_logging() and storage._predictions:
                for loader_i, table in enumerate(tables):
                    table_name = self._dataset[loader_i] + "_"+ str(loader_i)
                    self._use_table_as_artifact(table, table_name, loader_i)
                    log_dict[table_name] = table

            log_dict["predictions"] = self._media
            storage.clear_predictions()
                    
        for k, (v, _) in storage.latest_with_smoothing_hint(self._window_size).items():
            log_dict[k] = v

        self._run.log(log_dict)

    def _use_table_as_artifact(self, table, table_name, loader_i):
        '''
        This function logs the given table as artifact and calls `use_artifact` on it so tables from next iter-
        ations can use the reference of already uploaded images.
        '''
        if self._evalset_table[loader_i] is None:
            eval_art = wandb.Artifact(self._run.id + table_name, type="dataset")
            eval_art.add(table, table_name)
            self._run.use_artifact(eval_art)
            eval_art.wait()
            self._evalset_table[loader_i] = eval_art.get(table_name)


    def _table_logging(self):
        '''
        This function returns true if user defined settings enable tables logging implicitly or explicitly
        '''
        # TODO: put actual logic here
        return True

    def _build_evalset_tables(self):
        '''
        
        '''
        # The design of tables for each task is up for discussion. 
        # * If each prediction of a kind has its col, then there'll be 160 columns for coco detection + ins. seg. This isn't ideal
        # * Writer doesn't have access to any mask overlay metrics like IOUs or false +ves etc,
        #
        # Current design - Use cols. for each detection class score and don't use columns for mask overlays 
        tables = []
        for loader_i in range(self._num_loaders):
            table_cols = ["file_name", "image"] + self.thing_class_names[loader_i]
            table = wandb.Table(columns=table_cols)
            tables.append(table)

        return tables



    def close(self):
        self._run.finish()
