import argparse
from datetime import datetime
import json
import numpy as np
from pathlib import Path
import time
from tqdm import tqdm
import warnings

import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter

from data import data_loader
from model import RandLANet
from config import cfg

USE_CUDA = torch.cuda.is_available()
device = 'cuda' if USE_CUDA else 'cpu'


def evaluate(model, loader, criterion, desc=None):
    model.eval()
    losses = []
    accuracies = []
    with torch.no_grad():
        for points, labels in tqdm(loader, desc=desc, leave=False):
            pred = model(points)
            loss = criterion(pred.squeeze(), labels.squeeze())
            pred_labels = torch.argmax(pred[0], 1)
            correct = (pred_labels == labels[0]).float().sum()
            accuracies.append((correct/points.shape[1]).cpu().item())
            losses.append(loss.cpu().item())
    return np.mean(losses), np.mean(accuracies)


def train(args):
    train_path = args.dataset / args.train_dir
    val_path = args.dataset / args.val_dir
    logs_dir = args.logs_dir / args.name
    logs_dir.mkdir(exist_ok=True)

    # determine number of classes
    try:
        with open(args.dataset / 'classes.json') as f:
            labels = json.load(f)
            num_classes = len(labels.keys())
    except FileNotFoundError:
        num_classes = int(input("Number of distinct classes in the dataset: "))

    train_loader = data_loader(
        train_path,
        device=args.gpu,
        train=True,
        split='training',
        num_workers=args.num_workers
    )
    val_loader = data_loader(train_path, device=args.gpu, train=True, split='validation', batch_size=args.batch_size)
    d_in = 6 # next(iter(train_loader))[0].size(-1)

    model = RandLANet(
        d_in,
        num_classes,
        num_neighbors=args.neighbors,
        decimation=args.decimation,
        device=args.gpu
    )

    class_weights = np.array(cfg.class_weights)
    class_weights = torch.tensor((class_weights / float(sum(class_weights))).astype(np.float32)).to(args.gpu)
    criterion = nn.CrossEntropyLoss(weight=class_weights)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.adam_lr)
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, args.scheduler_gamma)

    first_epoch = 1
    if args.load:
        path = max(list((args.logs_dir / args.load).glob('*.pth')))
        print(f'Loading {path}...')
        checkpoint = torch.load(path)
        first_epoch = checkpoint['epoch']+1
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])

    with SummaryWriter(logs_dir) as writer:
        for epoch in range(first_epoch, args.epochs+1):
            t0 = time.time()
            # Train
            model.train()
            losses = []
            accuracies = []
            for points, labels in tqdm(train_loader, desc=f'[Epoch {epoch:d}/{args.epochs:d}]\tTraining', leave=False):
                optimizer.zero_grad()
                pred = model(points)
                loss = criterion(pred.squeeze(), labels.squeeze())

                loss.backward()

                optimizer.step()
                scheduler.step()

                losses.append(loss.cpu().item())
                pred_labels = torch.argmax(pred[0], 1)
                correct = (pred_labels == labels[0]).float().sum()
                accuracies.append((correct/points.shape[1]).cpu().item())

            val_loss, val_acc = evaluate(model, val_loader, criterion, desc=f'[Epoch {epoch:d}/{args.epochs:d}]\tValidation')

            loss_dic = {
                'Training loss':    np.mean(losses),
                'Validation loss': val_loss
            }

            accuracy_dic = {
                'Training accuracy':    np.mean(accuracies),
                'Validation accuracy':  val_acc
            }

            t1 = time.time()
            # Display results
            print(f'[Epoch {epoch:d}/{args.epochs:d}]', end='\t')
            for k, v in loss_dic.items():
                print(f'{k}: {v:.7f}', end='\t')

            for k, v in accuracy_dic.items():
                print(f'{k}: {v:.7f}', end='\t')
            d = t1 - t0
            print('\tTime elapsed:', '{:.0f} s'.format(d) if d < 60 else '{:.0f} min {:.0f} s'.format(*divmod(d, 60)))
            writer.add_scalars('Loss', loss_dic, epoch)
            writer.add_scalars('Accuracy', accuracy_dic, epoch)

            if epoch % args.save_freq == 0:
                torch.save(
                    dict(
                        epoch=epoch,
                        model_state_dict=model.state_dict(),
                        optimizer_state_dict=optimizer.state_dict(),
                        scheduler_state_dict=scheduler.state_dict()
                    ),
                    args.logs_dir / args.name / f'checkpoint_{epoch:d}.pth'
                )


if __name__ == '__main__':

    """Parse program arguments"""
    parser = argparse.ArgumentParser(
        prog='RandLA-Net',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    base = parser.add_argument_group('Base options')
    expr = parser.add_argument_group('Experiment parameters')
    param = parser.add_argument_group('Hyperparameters')
    dirs = parser.add_argument_group('Storage directories')
    misc = parser.add_argument_group('Miscellaneous')

    base.add_argument('--dataset', type=Path, help='location of the dataset',
                        default='datasets/s3dis/reprocessed')

    expr.add_argument('--epochs', type=int, help='number of epochs',
                        default=200)
    expr.add_argument('--load', type=str, help='model to load',
                        default='')

    param.add_argument('--adam_lr', type=float, help='learning rate of the optimizer',
                        default=1e-2)
    param.add_argument('--batch_size', type=int, help='batch size',
                        default=1)
    param.add_argument('--decimation', type=int, help='ratio the point cloud is divided by at each layer',
                        default=4)
    param.add_argument('--neighbors', type=int, help='number of neighbors considered by k-NN',
                        default=16)
    param.add_argument('--scheduler_gamma', type=float, help='gamma of the learning rate scheduler',
                        default=0.95)

    dirs.add_argument('--test_dir', type=str, help='location of the test set in the dataset dir',
                        default='test')
    dirs.add_argument('--train_dir', type=str, help='location of the training set in the dataset dir',
                        default='train')
    dirs.add_argument('--val_dir', type=str, help='location of the validation set in the dataset dir',
                        default='val')
    dirs.add_argument('--logs_dir', type=Path, help='path to tensorboard logs',
                        default='runs')

    misc.add_argument('--gpu', type=int, help='which GPU to use (-1 for CPU)',
                        default=0)
    misc.add_argument('--name', type=str, help='name of the experiment',
                        default=None)
    misc.add_argument('--num_workers', type=int, help='number of threads for loading data',
                        default=0)
    misc.add_argument('--save_freq', type=int, help='frequency of saving checkpoints',
                        default=10)

    args = parser.parse_args()

    if args.gpu >= 0:
        if torch.cuda.is_available():
            args.gpu = torch.device(f'cuda:{args.gpu:d}')
        else:
            warnings.warn('CUDA is not available on your machine. Running the algorithm on CPU.')
            args.gpu = torch.device('cpu')
    else:
        args.gpu = torch.device('cpu')

    if args.name is None:
        args.name = datetime.now().strftime('%Y-%m-%d_%H:%M')

    t0 = time.time()
    train(args)
    t1 = time.time()

    d = t1 - t0
    print('Done. Time elapsed:', '{:.0f} s.'.format(d) if d < 60 else '{:.0f} min {:.0f} s.'.format(*divmod(d, 60)))
