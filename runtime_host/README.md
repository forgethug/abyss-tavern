# 游戏运行服务 / Game runtime

每个酒馆使用独立进程和数据库，保存自己的角色、物品和世界记录。平台负责启动、停止和管理这些进程。

安装并启动根目录的 `tools/setup.py` 后，直接从平台创建和进入酒馆即可。日常启动使用 `tools/start.cmd`（Windows）或 `python3 tools/start.py`（Linux）。

- [中文部署教程](../docs/DEPLOYMENT.md)
- [English deployment guide](../docs/DEPLOYMENT.en.md)

Each tavern runs in its own process with its own database. The platform starts and manages these processes for you. Use the project setup and start scripts, then create or enter a tavern from the platform. See the deployment guides above for configuration, hosting and backups.
