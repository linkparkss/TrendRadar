# 潮流自动收敛工具

这是从工作区整理出的 PSASP 潮流收敛搜索与自动恢复工具。源码位于本目录，适用于 Python 3.11+。

## 功能范围

- 诊断普通潮流结果，按最少设备改动生成候选。
- 通过 `wmlfadj.exe` 执行受控计算，并严格检查结果文件新鲜度、Case 身份、最终失配量和母线越限。
- 将 Temp 文件和 PSASP MySQL 数据库动作绑定，失败时按快照和条件写入规则回滚。
- 通过样本目录进行离线校准、样本回放和多基线训练。
- 不自动点击 PSASP 图形界面；普通潮流复核仍需操作者在 PSASP 中完成。

## 可直接运行的程序

| 文件 | 用途 |
| --- | --- |
| [`潮流收敛.py`](潮流收敛.py) | 统一入口。无参数时运行传统并联补偿最少投切搜索；带参数时转入数据库同步、基线包或生成样本流程。 |
| [`潮流收敛_数据库同步版.py`](潮流收敛_数据库同步版.py) | 数据库字段和 Temp 输入同步后调用 wmlfadj，验证结果并在失败时同时回滚。 |
| [`潮流收敛_自动训练版.py`](潮流收敛_自动训练版.py) | 按一个已标注样本应用恢复动作，支持预览、实际运行和恢复历史快照。 |
| [`潮流收敛_样本回放.py`](潮流收敛_样本回放.py) | 重放故障样本、执行恢复动作，并验证恢复是否成立。 |
| [`训练样本校准.py`](训练样本校准.py) | 离线生成样本校准报告，从不启动 PSASP。 |
| [`通用潮流收敛.py`](通用潮流收敛.py) | 兼容性启动器，调用 `python -m powerflow.convergence` 的入口逻辑。 |

## 核心模块用途

这些模块由上面的程序调用，通常不需要单独运行。

| 模块 | 用途 |
| --- | --- |
| `adaptive_load.py` | 按负荷增加量寻找收敛边界的确定性自适应搜索。 |
| `assessment.py` | 为普通潮流结果增加 definitive/inconclusive 判定层。 |
| `automatic.py` | 自动恢复的公开 API 与字段动作定义。 |
| `automatic_impl.py` | 基于标注失败样本的 wmlfadj 自动恢复实现。 |
| `baseline_bundle.py` | 捕获、校验、切换和恢复精确的 PSASP Temp 基线包。 |
| `candidates.py` | 生成并排序最少改动的候选动作集合。 |
| `cli.py` | 手动 PSASP 闭环命令行入口，不操作 PSASP 图形界面。 |
| `config.py` | 读取并校验 TOML 配置，以及数据库密码环境变量。 |
| `diagnostics.py` | 解析 PSASP 迭代报告并生成搜索诊断信息。 |
| `generated_training.py` | 执行单基线的受控扰动样本训练流程。 |
| `generated_training_v2.py` | 执行多基线、带基线控制和回滚保护的样本训练流程。 |
| `journal.py` | 保存可崩溃恢复的 JSON 运行日志。 |
| `models.py` | 共享的案例、设备、动作、候选和验证结果数据模型。 |
| `repository.py` | 提供带预检和补偿回滚的数据库仓库公开接口。 |
| `repository_impl.py` | 实现 PSASP MySQL 表的条件写入与可逆更新。 |
| `sample_factory.py` | 根据 Temp 卡片生成受限的 N-1、设定值、负荷和组合扰动计划。 |
| `service.py` | 手动 PSASP 收敛搜索状态机，负责候选推进、验证和回滚。 |
| `temp_executor.py` | 对活动 Temp 输入文件执行样本恢复、快照和 wmlfadj 调用。 |
| `training.py` | 离线分析标注样本并生成校准目录，不运行求解器。 |
| `verifier.py` | 严格验证新鲜的普通 PSASP 潮流结果。 |
| `wmlfadj_result.py` | 解析并验证 wmlfadj.exe 输出及调整结果。 |
| `__init__.py` | 导出潮流收敛包的公共类型和函数。 |
| `__main__.py` | 支持 `python -m powerflow.convergence` 的模块启动入口。 |

## 配置和运行

1. 复制 `convergence.toml.example` 为 `convergence.toml`，填写本机 PSASP、Temp、案例和数据库信息。
2. 数据库密码只通过环境变量提供，例如 PowerShell：`$env:PSASP_DB_PASSWORD = "你的密码"`。
3. 先执行只读预览，确认 `case_matches` 或候选信息正确，再增加 `--run`。

样本恢复示例：

```powershell
python 潮流收敛_自动训练版.py --config convergence.toml --sample FC08_GEN_OUTAGE_LARGE
python 潮流收敛_自动训练版.py --config convergence.toml --sample FC08_GEN_OUTAGE_LARGE --run
```

手动 PSASP 闭环：

```powershell
python -m powerflow.convergence --config convergence.toml start
python -m powerflow.convergence --config convergence.toml next --run-dir "<run_dir>"
python -m powerflow.convergence --config convergence.toml verify --run-dir "<run_dir>"
```

传统统一入口示例：

```powershell
python 潮流收敛.py --config convergence.toml --sample FC08_GEN_OUTAGE_LARGE
```

## 样本与限制

- `samples/fengcheng08_training.json` 保存丰城三期08的首批标注样本。
- `samples/fengcheng08_training_profile.json` 是离线校准结果。
- 样本恢复规则是特定案例的经验规则，不保证适用于任意新故障。
- 涉及发电机有功、LCC 设定值、变压器分接头和负荷切除时，应先完成成功标签、上下限和回滚策略验证。
- 不要把真实数据库、Temp 卡片、`LF.CAL`、`LFCAL.LIS`、运行日志或密码提交到公开仓库。

## 安全执行原则

所有写入前应确认 PSASP 已关闭，目标 Case 与当前 Temp 身份一致。失败、超时、结果不新鲜、最终失配超容差或平衡节点越限，都应按程序流程回滚并人工检查。

详细的手动闭环说明见 [`powerflow/convergence/README.md`](powerflow/convergence/README.md)，样本说明见 [`samples/README.md`](samples/README.md)。
