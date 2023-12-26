from typing import Tuple
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange


ArrayType = np.ndarray | torch.Tensor


class ContextBase:
    """ Base class for context objects.
    A context is an object which, given a full context data 
    (e.g. prediction: past+future) can return only the in-context data
    (e.g. prediction: past) or only out-context data (e.g. prediction: future)

    :raises NotImplementedError: _description_
    :raises NotImplementedError: _description_
    :raises NotImplementedError: _description_
    :raises NotImplementedError: _description_
    """

    def select_in_context(self, x: ArrayType) -> ArrayType:
        raise NotImplementedError

    def select_out_context(self, x: ArrayType) -> ArrayType:
        raise NotImplementedError

    def pad_context(self, x_context: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError

    def get_out_times(self):
        raise NotImplementedError

#TODO: simplify the derived context classes to just defining a 2d mask
class PredictionContext(ContextBase):

    def __init__(self, 
                 horizon: int | None = None,
                 **kwargs):
        self.horizon = horizon

    def select_in_context(self, x: ArrayType) -> ArrayType:
        if self.horizon is None:
            return x
        return x[..., :-self.horizon]
    
    def select_out_context(self, x: ArrayType) -> ArrayType:
        if self.horizon is None:
            return x
        return x[..., -self.horizon:]

    def pad_context(self, x_context: torch.Tensor) -> torch.Tensor:
        if self.horizon is None:
            return x_context
        return F.pad(x_context, (0, self.horizon))

    def get_out_times(self):
        if self.horizon is None:
            return 0
        return self.horizon
    

class ImputationContext(ContextBase):
    
    def __init__(self, 
                 portion: Tuple | None = None):
        self.portion = portion

    def select_in_context(self, x: ArrayType) -> ArrayType:
        if self.portion is None:
            return x
        l, _, r = self.portion
        return np.concatenate([x[...,:l], x[..., -r:]], axis=-1)
    
    def slect_out_context(self, x: ArrayType) -> ArrayType:
        if self.portion is None:
            return x
        l, _, r = self.portion
        return x[..., l:-r]

    def pad_context(self, x_context: torch.Tensor) -> torch.Tensor:
        if self.portion is None:
            return x_context
        l, c, r = self.portion
        x_left = x_context[...,:l]
        x_right = x_context[...,-r:]
        zeros_middle = x_context.new_zeros(x_context.shape[:-1]+(c,))
        return torch.cat([x_left,zeros_middle,x_right], dim=-1)

    def get_out_times(self):
        if self.portion is None:
            return 0
        return self.portion[1]
    

class CrossChannelContext(ContextBase):
    def __init__(self, 
                 out_context_channels: int):
        self.out_context_channels = out_context_channels

    def select_in_context(self, x: ArrayType) -> ArrayType:
        in_context_channels = x.shape[-2] - self.out_context_channels
        return x[...,:in_context_channels,:]

    def select_out_context(self, x: ArrayType) -> ArrayType:
        if self.out_context_channels is None:
            return x
        return x[...,-self.out_context_channels:,:]
    
    def pad_context(self, x_context: torch.Tensor) -> torch.Tensor:
        if self.out_context_channels is None:
            return x_context
        new_shape = list(x_context.shape)
        new_shape[-2] = self.out_context_channels
        zeros_up = x_context.new_zeros(new_shape)
        return torch.cat([x_context,zeros_up], dim=-2)
    
    def get_out_times(self):
        return 0
    

class PathEmbedding(nn.Module):
    """ A class of linear embeddings. """

    def __init__(self, kernel: torch.Tensor):
        super().__init__()
        self.register_buffer("kernel", kernel)  # shape (embedding_dim, 1, kernel_size)
    
    def adjust_to_context(self, context):
        """ Adjust the kernel to the context. """
        new_kernel = context.pad_context(self.kernel)
        return PathEmbedding(new_kernel)
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.conv1d(x, self.kernel)
        x = rearrange(x, 'b d t -> b t d')
        return x


class Identity(PathEmbedding):

    def __init__(self):
        super().__init__(torch.empty(0))

    def adjust_to_context(self, mask_in_context):
        return Identity()

    def forward(self, x: np.ndarray):
        return x


class Foveal(PathEmbedding):
    """ A foveal embedding: This embedding technique captures the context 
    with varying levels of resolution. The closer the context is to the 
    focal point (fovea) e.g. the present time, the higher the resolution, 
    while the further away the context is, the lower the resolution. 
    This approach allows for efficient representation of the context, 
    with more emphasis on recent information. """

    def __init__(self, alpha: float, beta: float, max_context: int):
        self.alpha = alpha
        self.beta = beta
        self.max_context = max_context

        self.dim = int(np.floor(np.log(max_context) / np.log(alpha)))

        lengths = [int(alpha ** n) for n in range(1, 1 + self.dim)]
        self.slices = [slice(-le, None) for le in lengths]

        kernel = torch.zeros(self.dim, 1, max_context, dtype=torch.float32)

        for isl, sl in enumerate(self.slices):
            n = -sl.start
            kernel[isl, :, sl] = n ** (-beta)

        super().__init__(kernel)
