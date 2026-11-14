"""M3 torch model definitions — price forecasting models (L4 FNN, L5 CNN, L6 LSTM).

All models map to 48 half-hour regression outputs with a LINEAR head (no activation
on the final layer), so negative prices are possible. Training loss is L1 (MAE);
no cross-entropy/classification anywhere in the price path.
"""
from __future__ import annotations

import torch
from torch import nn


class FNN(nn.Module):
    """Lecture 4 feedforward net: flat features -> [128, 64] ReLU -> 48 outputs."""

    def __init__(self, in_dim: int, seed: int = 0):
        super().__init__()
        torch.manual_seed(seed)
        self.net = nn.Sequential(
            nn.Linear(in_dim, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 48),
        )

    def forward(self, x_flat: torch.Tensor) -> torch.Tensor:
        return self.net(x_flat)


class CNN1D(nn.Module):
    """Lecture 5 1D CNN on the (C, 96) past sequence + flat extras; linear head."""

    def __init__(self, channels: int, extras: int, seed: int = 0):
        super().__init__()
        torch.manual_seed(seed)
        self.conv = nn.Sequential(
            nn.Conv1d(channels, 8, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
            nn.Conv1d(8, 16, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
        )
        # (96 -> 48 -> 24) temporal positions flattened: 16 * 24
        self.head = nn.Sequential(
            nn.Linear(16 * 24 + extras, 128),
            nn.ReLU(),
            nn.Linear(128, 48),
        )

    def forward(self, seq: torch.Tensor, extra: torch.Tensor) -> torch.Tensor:
        h = self.conv(seq).flatten(1)          # seq: (B, C, 96)
        return self.head(torch.cat([h, extra], dim=1))


class LSTMNet(nn.Module):
    """Lecture 6 LSTM (prioritized over plain RNN) on the (96, C) sequence.

    Takes the final hidden state, appends flat extras, linear regression head.
    """

    def __init__(self, channels: int, extras: int, hidden: int = 32, seed: int = 0):
        super().__init__()
        torch.manual_seed(seed)
        self.lstm = nn.LSTM(input_size=channels, hidden_size=hidden,
                            num_layers=1, batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(hidden + extras, 128),
            nn.ReLU(),
            nn.Linear(128, 48),
        )

    def forward(self, seq: torch.Tensor, extra: torch.Tensor) -> torch.Tensor:
        # seq: (B, 96, C), batch_first
        _, (h_n, _) = self.lstm(seq)
        h = h_n[-1]                            # (B, hidden)
        return self.head(torch.cat([h, extra], dim=1))


def build_model(kind: str, channels: int, extras: int, seed: int = 0) -> nn.Module:
    if kind == "fnn":
        return FNN(channels * 96 + extras, seed=seed)
    if kind == "cnn":
        return CNN1D(channels, extras, seed=seed)
    if kind == "lstm":
        return LSTMNet(channels, extras, seed=seed)
    raise ValueError(f"unknown model kind: {kind}")
