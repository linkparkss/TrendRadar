# 丰城三期08首批校准样本

`fengcheng08_training.json` 包含本轮纳入的三个不收敛样本和一个正常基准：

- Case 24：正常基准
- Case 37：发电机 `Pg` 降至 80%
- Case 36：雅湖 LCC 直流功率由 650 万降至 500 万
- Case 34：停运大容量发电机 `赣丰城三期08-8`

Case 35（停运 `赣贵二期01-1`）按当前要求列在 `excluded_samples`，不参与本轮校准。

生成校准报告：

```powershell
python 训练样本校准.py
```

报告写入 `fengcheng08_training_profile.json`。该报告只用于离线诊断校准；当前
自动搜索器仍只允许 `Valid` 动作，`Pg` 和 LCC 直流功率被标记为
`diagnostic_only`，在没有成功恢复标签、上下限和回滚策略前不会自动修改。
