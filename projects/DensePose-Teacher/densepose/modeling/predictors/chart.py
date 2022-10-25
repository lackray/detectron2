# Copyright (c) Facebook, Inc. and its affiliates.

import torch
from torch import nn
import torch.nn.functional as F

from detectron2.config import CfgNode
from detectron2.layers import ConvTranspose2d, interpolate, Conv2d

from ...structures import DensePoseChartPredictorOutput
from ..utils import initialize_module_params
from .registry import DENSEPOSE_PREDICTOR_REGISTRY


@DENSEPOSE_PREDICTOR_REGISTRY.register()
class DensePoseChartPredictor(nn.Module):
    """
    Predictor (last layers of a DensePose model) that takes DensePose head outputs as an input
    and produces 4 tensors which represent DensePose results for predefined body parts
    (patches / charts):
     * coarse segmentation, a tensor of shape [N, K, Hout, Wout]
     * fine segmentation, a tensor of shape [N, C, Hout, Wout]
     * U coordinates, a tensor of shape [N, C, Hout, Wout]
     * V coordinates, a tensor of shape [N, C, Hout, Wout]
    where
     - N is the number of instances
     - K is the number of coarse segmentation channels (
         2 = foreground / background,
         15 = one of 14 body parts / background)
     - C is the number of fine segmentation channels (
         24 fine body parts / background)
     - Hout and Wout are height and width of predictions
    """

    def __init__(self, cfg: CfgNode, input_channels: int):
        """
        Initialize predictor using configuration options

        Args:
            cfg (CfgNode): configuration options
            input_channels (int): input tensor size along the channel dimension
        """
        super().__init__()
        dim_in = input_channels
        n_segm_chan = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        # coarse segmentation
        self.ann_index_lowres = ConvTranspose2d(
            dim_in, n_segm_chan, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # fine segmentation
        self.index_uv_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # U
        self.u_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # V
        self.v_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # Distribution Calibration
        self.ts_on = cfg.MODEL.SEMI.TS_ON
        if self.ts_on:
            self.ts_factor = ConvTranspose2d(
                dim_in, 1, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
            )

        hidden_dim = cfg.MODEL.SEMI.COR.CONV_HEAD_DIM
        kernel_size = cfg.MODEL.SEMI.COR.CONV_HEAD_KERNEL
        self.n_stacked_convs = cfg.MODEL.SEMI.COR.NUM_STACKED_CONVS
        pad_size = kernel_size // 2
        n_pred_channels = (cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1) + 2
        n_channels = n_pred_channels + 256 + 1  # + cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        for i in range(self.n_stacked_convs):
            layer = Conv2d(n_channels, hidden_dim, kernel_size, stride=1, padding=pad_size)
            layer_name = _get_layer_name(i)
            self.add_module(layer_name, layer)
            n_channels = hidden_dim
        self.n_out_channels = n_channels

        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE
        initialize_module_params(self)

    def interp2d(self, tensor_nchw: torch.Tensor):
        """
        Bilinear interpolation method to be used for upscaling

        Args:
            tensor_nchw (tensor): tensor of shape (N, C, H, W)
        Return:
            tensor of shape (N, C, Hout, Wout), where Hout and Wout are computed
                by applying the scale factor to H and W
        """
        return interpolate(
            tensor_nchw, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
        )

    def forward(self, head_outputs: torch.Tensor):
        """
        Perform forward step on DensePose head outputs

        Args:
            head_outputs (tensor): DensePose head outputs, tensor of shape [N, D, H, W]
        Return:
           An instance of DensePoseChartPredictorOutput
        """
        fine_segm = self.interp2d(self.index_uv_lowres(head_outputs))
        if self.ts_on:
            ts_factor = F.softplus(self.interp2d(self.ts_factor(head_outputs))) + 0.01
            fine_segm = fine_segm * torch.repeat_interleave(
                ts_factor, fine_segm.shape[1], dim=1
            )

        crt_output = head_outputs
        for i in range(self.n_stacked_convs):
            layer_name = self._get_layer_name(i)
            crt_output = getattr(self, layer_name)(crt_output)
            crt_output = F.relu(crt_output)

        output = DensePoseChartPredictorOutput(
            coarse_segm=self.interp2d(self.ann_index_lowres(head_outputs)),
            fine_segm=fine_segm,
            u=self.interp2d(self.u_lowres(head_outputs)),
            v=self.interp2d(self.v_lowres(head_outputs)),
            crt_segm=self.interp2d(crt_output)
        )
        return output


def _get_layer_name(i: int):
    layer_name = "body_conv_fcn{}".format(i + 1)
    return layer_name
