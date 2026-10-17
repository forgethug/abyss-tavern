# 来源与第三方组件

本项目原创代码按根目录 `LICENSE` 的 MIT 许可证发布。第三方组件各自的版权与许可证继续有效；MIT 声明不会覆盖第三方权利。

## 游戏包来源

- `game-packs/s1-fantasy/source-snapshot/SOURCE` 记录深渊酒馆上游提交；`PROJECTION.json` 记录自建版确定性导出，`TRACKED_FILES` 记录来源文件清单。
- `frontend-v2/src/plugins/adventurer_s1` 是 S1 快照前端的运行镜像；同步只由维护者执行，安装者不需要上游仓库。
- 模型服务使用条款由实际部署者与服务商约定。

## 依赖

Python 依赖固定在 `backend/requirements.txt`，前端解析版本固定在 `frontend-v2/package-lock.json`。这些依赖通过包管理器安装，源码快照不打包依赖目录。前端发布构建包含依赖代码时，应同时保留 `frontend-v2/dist/THIRD_PARTY_LICENSES.txt`；构建工具会从安装的依赖生成该文件，包括 Vue、Vue Router、Pinia、Axios、Ionicons 及相关间接依赖的许可证原文。不要仅复制压缩脚本而漏掉许可证文件。

更新依赖或新增素材时需要核对新增来源的授权；这里的说明不替代各组件的原始许可证。

## English

Original project code is released under the root MIT `LICENSE`. Third-party components retain their own copyrights and licenses. The game packs include source provenance metadata; installation does not require the upstream repository.

Python dependencies are listed in `backend/requirements.txt`, and frontend dependencies are locked in `frontend-v2/package-lock.json`. A frontend build generates `frontend-v2/dist/THIRD_PARTY_LICENSES.txt`; distribute that file with the built website. Model API services are subject to the provider's terms.
