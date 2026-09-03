# 通用潮流收敛搜索

## 统一运行入口

正式使用时只运行项目根目录的 `潮流收敛.py`。带配置和样本参数时，程序进入自动数据库调整、`wmlfadj.exe` 计算、结果验证及失败回滚闭环：

```powershell
python 潮流收敛.py --config convergence.toml --sample FC08_GEN_OUTAGE_LARGE --run
```

省略 `--run` 为只读预览，不启动 PSASP，也不修改数据库：

```powershell
python 潮流收敛.py --config convergence.toml --sample FC08_GEN_OUTAGE_LARGE
```

不带任何参数时仍执行原有的并联补偿最少投切搜索。`潮流收敛_自动训练版.py`、`潮流收敛_样本回放.py` 和 `潮流收敛_数据库同步版.py` 保留用于开发和兼容，不再作为日常使用入口。

该工具是一个可配置、可回滚、可恢复的搜索框架。它不启动 PSASP，不调用
PSASP 窗口，也不自动点击按钮；PSASP 的普通潮流计算由操作者完成，程序只负责
诊断、提出候选、修改数据库中的 `Valid`、读取结果并验证。

## 安装和配置

使用 Python 3.11 或更高版本。连接真实 PSASP 数据库时需要 `pymysql`。将
`convergence.toml.example` 复制为自己的 TOML 配置，至少修改数据库名、作业编号/名称
和 `temp_path`。数据库密码放在配置指定的环境变量中，例如 PowerShell：

```powershell
$env:PSASP_DB_PASSWORD = "你的数据库密码"
```

## 手动闭环

先关闭 PSASP（不要让旧的内存状态覆盖数据库），再建立搜索日志：

```powershell
python -m powerflow.convergence --config convergence.toml start
```

命令会输出一个 `run_dir`。每次只推进一个候选：

```powershell
python -m powerflow.convergence --config convergence.toml next --run-dir "<run_dir>"
```

然后由操作者在 PSASP 中重新打开工程、刷新/选择目标作业、运行普通潮流；应保存
本次普通潮流结果并关闭 PSASP。程序不代替这些操作。关闭后验证：

```powershell
python -m powerflow.convergence --config convergence.toml verify --run-dir "<run_dir>"
```

若验证发现 `LF.CAL`、`LFCAL.LIS` 旧、缺失或属于另一个作业，日志保持等待状态，
不会回滚，重新运行普通潮流后再验证。若结果是新鲜且明确不收敛，程序会进入
`rollback_required`；确认 PSASP 已关闭后回滚并进入下一个候选：

```powershell
python -m powerflow.convergence --config convergence.toml rollback --run-dir "<run_dir>" --psasp-closed
python -m powerflow.convergence --config convergence.toml next --run-dir "<run_dir>"
```

如果结果收敛，日志变为 `completed`，保留当前候选作为最终状态。所有数据库写入都
带有原值条件，回滚只接受日志记录的 `before/after` 状态；发现外部改动会停止并要求
人工检查。`run.json` 采用临时文件写入后替换，程序中断后重新执行同一命令即可继续
恢复或报告冲突。

## 验证口径

最终成功必须同时满足：新鲜的 `LF.CAL`、新鲜的 `LFCAL.LIS`、目标 `Case_No/Case_Name`、
`lf_case` 的计算状态和时间戳与 `LF.CAL` 一致，以及最后一次迭代的最大失配不超过
该作业容差。电压设定值、变压器分接头和负荷切除在当前版本明确禁止。

## 验证分级与候选排序

候选必须先满足硬约束，再比较调整设备数量。硬约束依次包括：潮流数值收敛、结果文件属于本次计算、最终失配不超过容差、平衡节点有功和无功均无星号越限。任何一项失败都必须回滚，不能因为设备数量更少而保留。

仅通过 wmlfadj.exe 的新候选标记为 provisional_converged，表示需要普通潮流复核；只有经过 PSASP GUI 普通潮流确认且无越限的动作集合，才标记为最终 converged。训练目录会保存普通潮流确认通过和确认失败的动作集合；后续回放优先使用已确认方案，并排除已经确认失败的候选。

数据库字段和 Temp 输入文件必须同步到同一候选状态后才能启动计算。候选失败时两者同时回滚，禁止使用基准 Temp 文件验证另一个数据库状态。

## 需要的多样化样本

首个“旋0”样本只能验证基本闭环，不能证明通用性。建议逐类提供：停机发电机、线路/变压器
退出、负荷水平变化、有功不平衡、无功越限导致 PV/PQ 切换、弱电网、孤岛、直流控制变化和
多设备同时故障。每个样本请保留原始算例/数据库备份、`LF.CAL`、`LFCAL.LIS`、
`lfreport.lis`、相关 LF 输入文件、已知成功的人工调整以及失败尝试（若有）。在未收集这些
样本前，不应把候选排序或“旋0”的两台发电机当作普遍规律。
