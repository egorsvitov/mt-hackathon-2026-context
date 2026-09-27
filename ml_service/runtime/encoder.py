"""TS2Vec inference-only adaptation. Copyright 2022 Zhihan Yue, MIT; see TS2VEC_LICENSE."""
import torch
from torch import nn
import torch.nn.functional as F
import numpy as np

class SamePadConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation=1, groups=1):
        super().__init__()
        self.receptive_field = (kernel_size - 1) * dilation + 1
        padding = self.receptive_field // 2
        self.conv = nn.Conv1d(
            in_channels, out_channels, kernel_size,
            padding=padding,
            dilation=dilation,
            groups=groups
        )
        self.remove = 1 if self.receptive_field % 2 == 0 else 0
        
    def forward(self, x):
        out = self.conv(x)
        if self.remove > 0:
            out = out[:, :, : -self.remove]
        return out
    
class ConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation, final=False):
        super().__init__()
        self.conv1 = SamePadConv(in_channels, out_channels, kernel_size, dilation=dilation)
        self.conv2 = SamePadConv(out_channels, out_channels, kernel_size, dilation=dilation)
        self.projector = nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels or final else None
    
    def forward(self, x):
        residual = x if self.projector is None else self.projector(x)
        x = F.gelu(x)
        x = self.conv1(x)
        x = F.gelu(x)
        x = self.conv2(x)
        return x + residual

class DilatedConvEncoder(nn.Module):
    def __init__(self, in_channels, channels, kernel_size):
        super().__init__()
        self.net = nn.Sequential(*[
            ConvBlock(
                channels[i-1] if i > 0 else in_channels,
                channels[i],
                kernel_size=kernel_size,
                dilation=2**i,
                final=(i == len(channels)-1)
            )
            for i in range(len(channels))
        ])
        
    def forward(self, x):
        return self.net(x)

class TSEncoder(nn.Module):
    def __init__(self, input_dims=7, output_dims=32, hidden_dims=32, depth=6):
        super().__init__()
        self.input_fc = nn.Linear(input_dims, hidden_dims)
        self.feature_extractor = DilatedConvEncoder(hidden_dims, [hidden_dims]*depth+[output_dims], kernel_size=3)
        self.repr_dropout = nn.Dropout(p=0.1)

    def forward(self, x):
        nan_mask = ~x.isnan().any(axis=-1)
        x = x.clone()
        x[~nan_mask] = 0
        x = self.input_fc(x)
        x[~nan_mask] = 0
        x = self.repr_dropout(self.feature_extractor(x.transpose(1, 2)))
        return x.transpose(1, 2)


class FrozenEncoder:
    def __init__(self, path, config, normalization):
        self.device = torch.device("cuda")
        self.mean = np.asarray(normalization["mean"], np.float32)
        self.scale = np.asarray(normalization["scale"], np.float32)
        net = TSEncoder(**{k:config[k] for k in ["input_dims", "output_dims", "hidden_dims", "depth"]}).to(self.device)
        self.net = torch.optim.swa_utils.AveragedModel(net)
        self.net.load_state_dict(torch.load(path, map_location=self.device, weights_only=True))
        self.net.eval()
        self.encode(np.zeros((1, 45, 7), np.float32))

    @torch.inference_mode()
    def encode(self, raw):
        count = len(raw)
        # Use the saved experiment's CUDA batch geometry for online requests:
        # batch=1 selects different convolution kernels and can cross tree thresholds.
        if count == 1:
            raw = np.repeat(raw, 128, axis=0)
        normalized = ((raw-self.mean)/self.scale).astype(np.float32)
        normalized[raw[:, :, 4] >= 1] = np.nan
        parts = []
        for start in range(0, len(raw), 128):
            x = torch.as_tensor(normalized[start:start+128], device=self.device)
            out = self.net(x)
            parts.append(F.max_pool1d(out.transpose(1, 2), kernel_size=out.size(1)).transpose(1, 2).squeeze(1).cpu().numpy())
        return np.concatenate(parts)[:count]
