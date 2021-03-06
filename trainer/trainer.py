# -*- coding: utf-8 -*-
# @Time    : 2018/8/23 22:20
# @Author  : zhoujun
import time
import Levenshtein
from tqdm import tqdm
import torch

from base import BaseTrainer


class Trainer(BaseTrainer):
    def __init__(self, config, model, criterion, train_loader, sample_input, converter, validate_loader=None):
        super().__init__(config, model, criterion, sample_input)
        self.train_loader = train_loader
        self.train_loader_len = len(train_loader)
        self.validate_loader = validate_loader
        self.converter = converter
        if self.validate_loader is not None:
            self.logger.info(f'train dataset has {self.train_loader.dataset_len} samples,{len(train_loader)} in dataloader,'
                             f'validate dataset has {self.validate_loader.dataset_len} samples,{len(self.validate_loader)} in dataloader')
        else:
            self.logger.info(f'train dataset has {len(self.train_loader.dataset)} samples,{len(self.train_loader)} in dataloader')

    def _train_epoch(self, epoch):
        self.model.train()
        epoch_start = time.time()
        batch_start = time.time()
        train_loss = 0.
        train_acc = 0.
        lr = self.optimizer.param_groups[0]['lr']
        for i, (images, labels) in enumerate(self.train_loader):
            if i >= self.train_loader_len:
                break
            self.global_step += 1
            lr = self.optimizer.param_groups[0]['lr']
            cur_batch_size = images.shape[0]
            targets, targets_lengths = self.converter.encode(labels, self.batch_max_length)
            images = images.to(self.device)
            targets = targets.to(self.device)

            # forward
            if self.model.prediction_type == 'CTC':
                preds = self.model(images)[0]
                preds = preds.log_softmax(2)
                preds_lengths = torch.tensor([preds.size(1)] * cur_batch_size, dtype=torch.long)
                loss = self.criterion(preds.permute(1, 0, 2), targets, preds_lengths, targets_lengths)  # text,preds_size must be cpu
            elif self.model.prediction_type == 'Attn':
                preds = self.model(images, targets[:, :-1])[0]
                target = targets[:, 1:]  # without [GO] Symbol
                loss = self.criterion(preds.view(-1, preds.shape[-1]), target.contiguous().view(-1))
            else:
                raise NotImplementedError
            # backward
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 5)
            self.optimizer.step()
            # loss 和 acc 记录到日志
            loss = loss.item()
            train_loss += loss

            batch_dict = self.accuracy_batch(preds, labels, phase='TRAIN')
            train_acc += batch_dict['n_correct']
            acc = batch_dict['n_correct'] / cur_batch_size
            edit_dis = batch_dict['edit_dis'] / cur_batch_size

            if self.use_tensorboard:
                # write tensorboard
                self.writer.add_scalar('TRAIN/ctc_loss', loss, self.global_step)
                self.writer.add_scalar('TRAIN/acc', acc, self.global_step)
                self.writer.add_scalar('TRAIN/edit_distance', edit_dis, self.global_step)
                self.writer.add_scalar('TRAIN/lr', lr, self.global_step)

            if (i + 1) % self.display_interval == 0:
                batch_time = time.time() - batch_start
                speed = self.display_interval * cur_batch_size / batch_time
                self.logger.info(f'[{epoch}/{self.epochs}], [{i + 1}/{self.train_loader_len}], global_step: {self.global_step}, '
                                 f'Speed: {speed:.1f} samples/sec, acc:{acc:.4f}, loss:{loss:.4f}, edit_dis:{edit_dis:.4f} lr:{lr}, time:{batch_time:.2f}')
                batch_start = time.time()
        return {'train_loss': train_loss / self.train_loader_len, 'time': time.time() - epoch_start, 'epoch': epoch,
                'lr': lr,
                'train_acc': train_acc / self.train_loader.dataset_len}

    def _eval(self, max_step=None, dest='test model'):
        self.model.eval()
        n_correct = 0
        edit_dis = 0
        for i, (images, labels) in enumerate(tqdm(self.validate_loader, desc=dest)):
            if max_step is not None and i >= max_step:
                break
            images = images.to(self.device)
            with torch.no_grad():
                preds = self.model(images)[0]
            batch_dict = self.accuracy_batch(preds, labels, phase='VAL')
            n_correct += batch_dict['n_correct']
            edit_dis += batch_dict['edit_dis']
        return {'n_correct': n_correct, 'edit_dis': edit_dis}

    def _on_epoch_finish(self):
        self.logger.info(f"[{self.epoch_result['epoch']}/{self.epochs}], train_acc: {self.epoch_result['train_acc']:.4f}, \ "
                         f"train_loss: {self.epoch_result['train_loss']:.4f}, time: {self.epoch_result['time']:.4f}, lr: {self.epoch_result['lr']}")
        net_save_path = f'{self.checkpoint_dir}/model_latest.pth'

        save_best = False
        if self.validate_loader is not None:
            epoch_eval_dict = self._eval()

            val_acc = epoch_eval_dict['n_correct'] / self.validate_loader.dataset_len
            edit_dis = epoch_eval_dict['edit_dis'] / self.validate_loader.dataset_len

            if self.use_tensorboard:
                self.writer.add_scalar('EVAL/acc', val_acc, self.global_step)
                self.writer.add_scalar(
                    'EVAL/edit_distance', edit_dis, self.global_step)

            self.logger.info(f"[{self.epoch_result['epoch']}/{self.epochs}], val_acc: {val_acc:.6f}")

            if val_acc >= self.metrics['val_acc']:
                save_best = True
                self.metrics['val_acc'] = val_acc
                self.metrics['train_loss'] = self.epoch_result['train_loss']
                self.metrics['train_acc'] = self.epoch_result['train_acc']
                self.metrics['best_model'] = net_save_path
                self.metrics['best_model_epoch'] = self.epoch_result['epoch']
        else:
            if self.epoch_result['train_acc'] > self.metrics['train_acc']:
                save_best = True
                self.metrics['train_loss'] = self.epoch_result['train_loss']
                self.metrics['train_acc'] = self.epoch_result['train_acc']
                self.metrics['best_model'] = net_save_path
                self.metrics['best_model_epoch'] = self.epoch_result['epoch']
        self._save_checkpoint(self.epoch_result['epoch'], net_save_path, save_best)

    def accuracy_batch(self, predictions, labels, phase):
        n_correct = 0
        edit_dis = 0.0
        predictions = predictions.softmax(dim=2).detach().cpu().numpy()
        preds_str = self.converter.decode(predictions)
        logged = False
        for (pred, pred_conf), target in zip(preds_str, labels):
            if self.use_tensorboard and not logged:
                self.writer.add_text(tag=f'{phase}/pred', text_string=f'pred: {pred} -- gt:{target}', global_step=self.global_step)
                logged = True
            edit_dis += Levenshtein.distance(pred, target)
            if pred == target:
                n_correct += 1
        return {'n_correct': n_correct, 'edit_dis': edit_dis}

    def _on_train_finish(self):
        for k, v in self.metrics.items():
            self.logger.info(f'{k}:{v}')
        self.logger.info('finish train')
