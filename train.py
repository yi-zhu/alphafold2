import torch
from torch.optim import Adam
from torch.utils.data import DataLoader
import torch.nn.functional as F
from einops import rearrange

from alphafold2_pytorch import Alphafold2

import sidechainnet as scn

# constants

NUM_BATCHES = int(1e5)
GRADIENT_ACCUMULATE_EVERY = 16
LEARNING_RATE = 3e-4
IGNORE_INDEX = -100
THRESHOLD_LENGTH = 250

# helpers

def cycle(loader, cond = lambda x: True):
    while True:
        for data in loader:
            if not cond(data):
                continue
            yield data

def derive_mask(coords):
    coords_center = coords[:, :, :2].mean(dim = 2) # mean of coordinates of N, Cα, C
    return coords_center.sum(dim = -1) == 0

def get_bucketed_distance_matrix(coords):
    coords_center = coords[:, :, :2].mean(dim = 2) # mean of coordinates of N, Cα, C
    distances = ((coords_center[:, :, None, :] - coords_center[:, None, :, :]) ** 2).sum(dim = -1).sqrt()
    boundaries = torch.linspace(2, 20, steps = 37, device = coords.device)
    return torch.bucketize(distances, boundaries)

# get data

data = scn.load(
    casp_version = 12,
    thinning = 30,
    with_pytorch = 'dataloaders',
    batch_size = 1,
    dynamic_batching = False
)

data = iter(data['train'])
data_cond = lambda t: t[1].shape[1] < THRESHOLD_LENGTH
dl = cycle(data, data_cond)

# model

model = Alphafold2(
    dim = 256,
    depth = 1,
    heads = 8,
    dim_head = 64
).cuda()

# optimizer

optim = Adam(model.parameters(), lr = LEARNING_RATE)

# training loop

for _ in range(NUM_BATCHES):
    for _ in range(GRADIENT_ACCUMULATE_EVERY):
        _, seq, *_, coords = next(dl)
        b, l = seq.shape

        # prepare mask, labels

        seq, coords = seq.cuda(), coords.cuda()
        coords = rearrange(coords, 'b (l c) d -> b l c d', l = l)
        mask = derive_mask(coords)

        discretized_distances = get_bucketed_distance_matrix(coords)
        discretized_distances.masked_fill_(mask[:, :, None] | mask[:, None, :], IGNORE_INDEX)

        # predict

        distogram = model(
            seq,
            mask = ~mask
        )

        distogram = rearrange(distogram, 'b i j c -> b c i j')

        # loss

        loss = F.cross_entropy(
            distogram,
            discretized_distances,
            ignore_index = IGNORE_INDEX
        )

        loss.backward()

    print('loss:', loss.item())
    optim.step()
    optim.zero_grad()
