# 深渊酒馆游戏包

深渊酒馆是一款 AI 驱动的奇幻文字 RPG，包含组队探险、角色养成、地下城与奇遇、酒馆建筑、委托和交易等玩法。可以独自游玩，也可以邀请朋友加入同一间酒馆。

- [玩法与默认设置](SELF_HOSTED_RULES.md)
- [安装、部署与模型配置](../../docs/DEPLOYMENT.md)
- [English playing guide](PLAYING.en.md)

## 目录说明

- `source-snapshot/`：游戏源码快照，来源及文件校验信息记录在 `SOURCE`、`PROJECTION.json` 和 `TRACKED_FILES` 中。
- `runtime_adapter/`：游戏包与平台的运行适配及默认配置。

每个酒馆使用独立进程和数据库，账号与成员资格由平台统一管理。安装者直接使用本仓库即可，部署步骤见上方教程。
