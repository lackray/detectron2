# Copyright (c) Facebook, Inc. and its affiliates.

import torch
from dataclasses import make_dataclass
from functools import lru_cache
from typing import Any, Optional


@lru_cache(maxsize=None)
def decorate_cse_predictor_output_class_with_confidences(BasePredictorOutput: type) -> type:
    """
    Create a new output class from an existing one by adding new attributes
    related to confidence estimation:
    - coarse_segm_confidence (tensor)

    Details on confidence estimation parameters can be found in:
    N. Neverova, D. Novotny, A. Vedaldi "Correlated Uncertainty for Learning
        Dense Correspondences from Noisy Labels", p. 918--926, in Proc. NIPS 2019
    A. Sanakoyeu et al., Transferring Dense Pose to Proximal Animal Classes, CVPR 2020

    The new class inherits the provided `BasePredictorOutput` class,
    it's name is composed of the name of the provided class and
    "WithConfidences" suffix.

    Args:
        BasePredictorOutput (type): output type to which confidence data
            is to be added, assumed to be a dataclass
    Return:
        New dataclass derived from the provided one that has attributes
        for confidence estimation
    """

    PredictorOutput = make_dataclass(
        BasePredictorOutput.__name__ + "WithConfidences",
        fields=[
            ("coarse_segm_confidence", Optional[torch.Tensor], None),
        ],
        bases=(BasePredictorOutput,),
    )

    # add possibility to index PredictorOutput

    def slice_if_not_none(data, item):
        if data is None:
            return None
        if isinstance(item, int):
            return data[item].unsqueeze(0)
        return data[item]

    def PredictorOutput_getitem(self, item):
        PredictorOutput = type(self)
        base_predictor_output_sliced = super(PredictorOutput, self).__getitem__(item)
        return PredictorOutput(
            **base_predictor_output_sliced.__dict__,
            coarse_segm_confidence=slice_if_not_none(self.coarse_segm_confidence, item),
        )

    PredictorOutput.__getitem__ = PredictorOutput_getitem

    def PredictorOutput_to(self, device: torch.device):
        """
        Transfers all tensors to the given device
        """
        PredictorOutput = type(self)
        base_predictor_output_to = super(PredictorOutput, self).to(device)  # pyre-ignore[16]

        def to_device_if_tensor(var: Any):
            if isinstance(var, torch.Tensor):
                return var.to(device)
            return var

        return PredictorOutput(
            **base_predictor_output_to.__dict__,
            coarse_segm_confidence=to_device_if_tensor(self.coarse_segm_confidence),
        )

    PredictorOutput.to = PredictorOutput_to
    return PredictorOutput
