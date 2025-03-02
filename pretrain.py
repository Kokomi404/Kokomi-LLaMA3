import os
import argparse
import time
import math
import torch
from torch import optim, nn
from contextlib import nullcontext
from transformers import AutoTokenizer
from torch.utils.data import DataLoader
from model import Kokomi
from Config import LLMConfig
from dataset import PretrainDataset

def get_lr(current_step, total_steps, lr):
    return lr / 10 + 0.5 * lr * (1 + math.cos(math.pi * current_step / total_steps))

def train_epoch(epoch, wandb):
     loss_fct = nn.CrossEntropyLoss(reduction='none')  # 定义损失函数，使用交叉熵损失

     start_time = time.time()  # 记录训练开始时间

     for step, (X, Y, loss_mask) in enumerate(train_loader):
          X = X.to(args.device)  # 将输入数据移动到设备上（GPU/CPU）
          Y = Y.to(args.device)  # 将标签数据移动到设备上
          loss_mask = loss_mask.to(args.device)  # 将损失mask数据移动到设备上

          lr = get_lr(epoch * iter_per_epoch + step, args.epochs * iter_per_epoch, args.learning_rate)
          for param_group in optimizer.param_groups:
               param_group['lr'] = lr

          with ctx:
               res = model(X)  # 模型的前向传播
               loss = loss_fct(
                    res.logits.view(-1, res.logits.size(-1)),  # 计算损失
                    Y.view(-1)
               ).view(Y.size())
               loss = (loss * loss_mask).sum() / loss_mask.sum()  # 应用mask并计算平均损失
               loss = loss / args.accumulation_steps  # 进行梯度累积

          scaler.scale(loss).backward()
          if (step + 1) % args.accumulation_steps == 0:
               scaler.unscale_(optimizer)  # 解除梯度缩放
               torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)  # 梯度裁剪

               scaler.step(optimizer)  # 执行优化步骤
               scaler.update()  # 更新缩放器
               optimizer.zero_grad(set_to_none=True)  # 清空梯度

          if step % args.log_step == 0:
               spend_time = time.time() - start_time  # 计算当前的时间
               print(
                    'Epoch:[{}/{}]({}/{}) loss:{:.3f} lr:{:.12f} epoch_Time:{}min:'.format(
                         epoch + 1,  # 当前epoch
                         args.epochs,  # 总的epoch数
                         step,  # 当前步数
                         iter_per_epoch,  # 每个epoch的迭代次数
                         loss.item() * args.accumulation_steps,  # 损失值，乘以累积步数
                         optimizer.param_groups[-1]['lr'],  # 当前学习率
                         spend_time / (step + 1) * iter_per_epoch // 60 - spend_time // 60  # 估计剩余时间
                         )
               )
               if (wandb is not None):
                    wandb.log({"loss": loss.item() * args.accumulation_steps,
                               "lr": optimizer.param_groups[-1]['lr'],
                               "epoch_Time": spend_time / (step + 1) * iter_per_epoch // 60 - spend_time // 60})

          if (step + 1) % args.save_step == 0:
               model.eval()  # 设置模型为评估模式
               ckp = f'{args.save_dir}/pretrain_step{step}.pth'  # 保存模型的路径

               state_dict = model.state_dict()

               torch.save(state_dict, ckp)  # 保存模型
               model.train()  # 恢复为训练模式


def init_model(llm_config):
     tokenizer = AutoTokenizer.from_pretrained('./Kokomi_tokenizer')  # 加载分词器
     model = Kokomi(llm_config).to(args.device)  # 初始化模型并移动到设备
     print(f'LLM总参数量：{sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6:.3f} 百万')  # 打印模型参数量
     return model, tokenizer


if __name__=='__main__':
     parser = argparse.ArgumentParser()

     parser.add_argument("--save_dir", type=str, default="results")  # 保存结果的目录
     parser.add_argument("--epochs", type=int, default=1)  # 训练的轮数
     parser.add_argument("--batch_size", type=int, default=80)  # 每批次的样本数量
     parser.add_argument("--learning_rate", type=float, default=5e-4)  # 学习率
     parser.add_argument("--device", type=str, default="cuda:0" if torch.cuda.is_available() else "cpu")  # 设备类型，支持cuda或cpu
     parser.add_argument("--use_wandb", type=bool,default=True)  # 是否使用wandb进行日志记录
     parser.add_argument("--dtype", type=str, default="bfloat16")  # 数据类型，默认使用bfloat16
     parser.add_argument("--wandb_project", type=str, default="Kokomib-Pretrain")  # wandb项目名称
     parser.add_argument("--num_workers", type=int, default=1)  # 数据加载时的工作线程数
     parser.add_argument("--accumulation_steps", type=int, default=2)  # 梯度累积步数
     parser.add_argument("--grad_clip", type=float, default=1.0)  # 梯度裁剪阈值
     parser.add_argument("--warmup_iters", type=int, default=0)  # 学习率预热的迭代次数
     parser.add_argument("--log_step", type=int, default=10)  # 每多少步记录一次日志
     parser.add_argument("--save_step", type=int, default=1000)  # 每多少步保存一次模型
     parser.add_argument('--max_seq_len', default=512, type=int)  # 输入的最大序列长度
     parser.add_argument("--data_path", type=str, default="pretrain.jsonl")  # 训练数据的路径
     
     args = parser.parse_args()

     lm_config = LLMConfig(max_seq_len=args.max_seq_len)
     
     args.save_dir = os.path.join(args.save_dir)
     os.makedirs(args.save_dir, exist_ok=True)

     tokens_per_iter = args.batch_size * lm_config.max_seq_len

     torch.manual_seed(1337)

     device_type = "cuda" if "cuda" in args.device else "cpu"

     args.wandb_run_name = f"Kokomi-Pretrain-Epoch-{args.epochs}-BatchSize-{args.batch_size}-LearningRate-{args.learning_rate}"
     
     ctx = nullcontext() if device_type == "cpu" else torch.cuda.amp.autocast()

     if args.use_wandb:
          import wandb

          wandb.init(project=args.wandb_project, name=args.wandb_run_name)
     else:
          wandb = None  # 如果不使用wandb，设置为None
     
     model, tokenizer = init_model(lm_config)

     train_ds = PretrainDataset(args.data_path, tokenizer, max_length=lm_config.max_seq_len)

     train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        pin_memory=True,  # 是否将数据复制到CUDA内存
        drop_last=False,  # 不丢弃最后一批数据
        shuffle=False,  # 不对数据进行乱序
        num_workers=args.num_workers,  # 数据加载时使用的子线程数
     )

     scaler = torch.cuda.amp.GradScaler(enabled=(args.dtype in ['float16', 'bfloat16']))

     optimizer = optim.AdamW(model.parameters(), lr=args.learning_rate)

     iter_per_epoch = len(train_loader)

     for epoch in range(args.epochs):
        train_epoch(epoch, wandb)