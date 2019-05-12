#!/usr/bin/env python

import multiprocessing
import os
import time

import numpy as np
import pandas as pd

from typing import Any, Optional, Tuple
from zipfile import ZipFile

from sklearn.preprocessing import LabelEncoder

import torch
import torch.nn as nn
import torchvision
import torchvision.transforms as transforms
import torch.backends.cudnn as cudnn

from torch.utils.data import TensorDataset, DataLoader, Dataset
from PIL import Image
from tqdm import tqdm

IN_KERNEL = os.environ.get('KAGGLE_WORKING_DIR') is not None
MIN_SAMPLES_PER_CLASS = 50
BATCH_SIZE = 512
LEARNING_RATE = 1e-2
LR_STEP = 3
LR_FACTOR = 0.3
NUM_WORKERS = multiprocessing.cpu_count()
MAX_STEPS_PER_EPOCH = 15000
NUM_EPOCHS = 1
LOG_FREQ = 50
NUM_TOP_PREDICTS = 20

class ImageDataset(torch.utils.data.Dataset):
    def __init__(self, dataframe: pd.DataFrame, mode: str) -> None:
        print(f'creating data loader - {mode}')
        assert mode in ['train', 'val', 'test']

        self.df = dataframe
        self.mode = mode
        self.zipfile = ZipFile(f'../input/{mode}.zip')

        self.transforms = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                  std=[0.229, 0.224, 0.225]),
        ])

    def __getitem__(self, index: int) -> Any:
        ''' Returns: tuple (sample, target) '''
        filename = self.df.id.values[index]

        sample = Image.open(self.zipfile.open(f'{self.mode}_64/{filename}.jpg'))
        assert sample.mode == 'RGB'

        image = np.array(sample)
        assert image.dtype == np.uint8

        image = self.transforms(image)

        if self.mode == 'test':
            return image
        else:
            return image, self.df.landmark_id.values[index]

    def __len__(self) -> int:
        return self.df.shape[0]

def GAP(predicts: torch.Tensor, confs: torch.Tensor, targets: torch.Tensor) -> float:
    ''' Simplified GAP@1 metric: only one prediction per sample is supported '''
    assert len(predicts.shape) == 1
    assert len(confs.shape) == 1
    assert len(targets.shape) == 1
    assert predicts.shape == confs.shape and confs.shape == targets.shape

    sorted_confs, indices = torch.sort(confs, descending=True)

    confs = confs.cpu().numpy()
    predicts = predicts[indices].cpu().numpy()
    targets = targets[indices].cpu().numpy()

    res, true_pos = 0.0, 0

    for i, (c, p, t) in enumerate(zip(confs, predicts, targets)): # type: ignore
        rel = int(p == t)
        true_pos += rel

        res += true_pos / (i + 1) * rel

    res /= targets.shape[0] # FIXME: incorrect, not all test images depict landmarks
    return res

class AverageMeter(object):
    ''' Computes and stores the average and current value '''
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.val = 0.0
        self.avg = 0.0
        self.sum = 0.0
        self.count = 0

    def update(self, val: float, n: int = 1) -> None:
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def load_data() -> Any:
    torch.multiprocessing.set_sharing_strategy('file_system')
    cudnn.benchmark = True

    df = pd.read_csv('../input/train.csv')
    df.drop(columns='url', inplace=True)

    # only use classes which have at least MIN_SAMPLES_PER_CLASS samples
    counts = df.landmark_id.value_counts()
    selected_classes = counts[counts >= MIN_SAMPLES_PER_CLASS].index
    num_classes = selected_classes.shape[0]
    print('classes with at least N samples:', num_classes)

    train_df = df.loc[df.landmark_id.isin(selected_classes)].copy()
    print('train_df', train_df.shape)

    test_df = pd.read_csv('../input/test.csv', dtype=str)
    test_df.drop(columns='url', inplace=True)
    print('test_df', test_df.shape)

    # filter non-existing test images
    with ZipFile('../input/test.zip') as zf:
        image_list = set(zf.namelist())

    test_df = test_df.loc[test_df.id.apply(lambda img: f'test_64/{img}.jpg' in image_list)].copy()
    print('test_df after filtering', test_df.shape)
    assert test_df.shape[0] > 112000

    label_encoder = LabelEncoder()
    label_encoder.fit(train_df.landmark_id.values)
    print('found classes', len(label_encoder.classes_))
    assert len(label_encoder.classes_) == num_classes

    train_df.landmark_id = label_encoder.transform(train_df.landmark_id)

    train_dataset = ImageDataset(train_df, mode='train')
    test_dataset = ImageDataset(test_df, mode='test')

    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE,
                              shuffle=False, num_workers=NUM_WORKERS, drop_last=True)

    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE,
                             shuffle=False, num_workers=NUM_WORKERS)

    return train_loader, test_loader, label_encoder, num_classes

def train(train_loader: Any, model: Any, criterion: Any, optimizer: Any,
          epoch: int, lr_scheduler: Any) -> None:
    print(f'epoch {epoch}')
    batch_time = AverageMeter()
    losses = AverageMeter()
    avg_score = AverageMeter()

    model.train()
    num_steps = min(len(train_loader), MAX_STEPS_PER_EPOCH)

    print(f'total batches: {num_steps}')

    end = time.time()
    lr_str = ''

    for i, (input_, target) in enumerate(train_loader):
        if i >= num_steps:
            break

        # compute output
        output = model(input_.cuda())
        loss = criterion(output, target.cuda())

        # get metric
        confs, predicts = torch.max(output.detach(), dim=1)
        avg_score.update(GAP(predicts, confs, target))

        # compute gradient and do SGD step
        losses.update(loss.data.item(), input_.size(0))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % LOG_FREQ == 0:
            print(f'{epoch} [{i}/{num_steps}]\t'
                        f'time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        f'loss {losses.val:.4f} ({losses.avg:.4f})\t'
                        f'GAP {avg_score.val:.4f} ({avg_score.avg:.4f})'
                        + lr_str)

    print(f' * average GAP on train {avg_score.avg:.4f}')

def inference(data_loader: Any, model: Any) -> Tuple[torch.Tensor, torch.Tensor,
                                                     Optional[torch.Tensor]]:
    ''' Returns predictions and targets, if any. '''
    model.eval()

    activation = nn.Softmax(dim=1)
    all_predicts, all_confs, all_targets = [], [], []

    with torch.no_grad():
        for i, data in enumerate(tqdm(data_loader, disable=IN_KERNEL)):
            if data_loader.dataset.mode != 'test':
                input_, target = data
            else:
                input_, target = data, None

            output = model(input_.cuda())
            output = activation(output)

            confs, predicts = torch.topk(output, NUM_TOP_PREDICTS)
            all_confs.append(confs)
            all_predicts.append(predicts)

            if target is not None:
                all_targets.append(target)

    predicts = torch.cat(all_predicts)
    confs = torch.cat(all_confs)
    targets = torch.cat(all_targets) if len(all_targets) else None

    return predicts, confs, targets

def generate_submission(test_loader: Any, model: Any, label_encoder: Any) -> np.ndarray:
    sample_sub = pd.read_csv('../input/recognition_sample_submission.csv')

    predicts_gpu, confs_gpu, _ = inference(test_loader, model)
    predicts, confs = predicts_gpu.cpu().numpy(), confs_gpu.cpu().numpy()

    labels = [label_encoder.inverse_transform(pred) for pred in predicts]
    print('labels')
    print(np.array(labels))
    print('confs')
    print(np.array(confs))

    sub = test_loader.dataset.df
    def concat(label: np.ndarray, conf: np.ndarray) -> str:
        return ' '.join([f'{L} {c}' for L, c in zip(label, conf)])
    sub['landmarks'] = [concat(label, conf) for label, conf in zip(labels, confs)]

    sample_sub = sample_sub.set_index('id')
    sub = sub.set_index('id')
    sample_sub.update(sub)

    sample_sub.to_csv('submission.csv')

if __name__ == '__main__':
    train_loader, test_loader, label_encoder, num_classes = load_data()

    model = torchvision.models.resnet50(pretrained=True)
    model.avg_pool = nn.AdaptiveAvgPool2d(1)
    model.fc = nn.Linear(model.fc.in_features, num_classes)
    model.cuda()

    criterion = nn.CrossEntropyLoss()

    optimizer = torch.optim.SGD(model.parameters(), lr=LEARNING_RATE)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=LR_STEP,
                                                   gamma=LR_FACTOR)

    for epoch in range(1, NUM_EPOCHS + 1):
        print('-' * 50)
        train(train_loader, model, criterion, optimizer, epoch, lr_scheduler)
        lr_scheduler.step()

    print('inference mode')
    generate_submission(test_loader, model, label_encoder)

