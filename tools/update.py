"""Interactive, offline upgrade entry point. No credentials are sent to GitHub."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from updater import (baseline, digest, download_release, internal_path, latest_release,
                     read_json, recover, upgrade, version)

ROOT = Path(__file__).resolve().parents[1]


def active_python(root):
    receipt = root / '.updates/current.json'
    if receipt.exists():
        relative = Path(read_json(receipt)['python'])
        path = internal_path(root, relative.parent) / relative.name
        if relative.name not in {'python', 'python.exe'} or not path.parent.is_relative_to(root / '.updates'):
            raise ValueError('Invalid installed environment')
        return path
    return root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def main(argv=None):
    parser = argparse.ArgumentParser(description='深渊酒馆更新：保留配置和玩家数据，更新前自动备份')
    parser.add_argument('--root', type=Path, default=ROOT, help='已安装酒馆的目录')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--check', action='store_true', help='仅检查正式更新，不修改安装')
    parser.add_argument('--yes', action='store_true', help='确认升级（服务仍必须停止）')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--package', type=Path, help='离线正式升级包')
    parser.add_argument('--sha256', help='离线包发布页提供的 SHA-256')
    parser.add_argument('--inside-env', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.check and not active_python(root).is_file() and not args.yes:
        root = Path(input('请输入已安装酒馆的文件夹路径（例如 D:\\abyss-tavern）：').strip().strip('"')).resolve()
    env_file = (args.env_file or Path(os.getenv('PLATFORM_ENV_FILE', str(root / '.env')))).resolve()
    if not args.check and not args.inside_env:
        python = active_python(root)
        if not python.is_file() or not env_file.is_file():
            raise ValueError('未找到已安装环境或私有配置，请先确认酒馆目录。')
        forwarded = list(argv if argv is not None else sys.argv[1:])
        return subprocess.call([str(python), '-X', 'utf8', str(Path(__file__).resolve()),
                                *forwarded, '--root', str(root), '--env-file', str(env_file), '--inside-env'])
    pending = (root / '.updates/pending.json').exists()
    if args.check:
        if pending:
            print('上次升级未完成，请双击 tools/update.cmd 恢复。')
            return 1
        current = baseline(root)
        release = latest_release()
        print('已安装版本：' + current['version'])
        print('最新正式版本：' + release['version'] if release else '暂无正式升级包，安装未作修改。')
        return 0
    os.environ['PLATFORM_ENV_FILE'] = str(env_file)
    sys.path[:0] = [str(root / 'backend'), str(root)]
    from config import Config
    from runtime_core.process_lock import instance_lock
    # Use the installed version's database backup implementation.
    spec = importlib.util.spec_from_file_location('installed_backup', root / 'tools/backup.py')
    backup_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup_module)
    # The original release predates backup_locked; use this updater's identical
    # backup implementation while retaining the installed Config/database paths.
    if not hasattr(backup_module, 'backup_locked'):
        import backup as backup_module
    maintenance = internal_path(root, '.updates')
    maintenance.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        maintenance.chmod(0o700)
    with instance_lock(maintenance), instance_lock(Config.RUNTIME_DATA_DIR):
        if recover(root):
            print('已完成中断恢复。原配置和玩家数据库未被替换；请重新运行更新以继续升级。')
            return 0
        current = baseline(root)
        if args.package:
            if not args.sha256 or digest(args.package) != args.sha256.lower():
                raise ValueError('离线包 SHA-256 校验失败。')
            archive, release = args.package.resolve(), None
        else:
            release = latest_release()
            if not release:
                print('暂无正式升级包，安装未作修改。')
                return 0
            if version(release['version']) <= version(current['version']):
                print('当前已是最新正式版本，安装未作修改。')
                return 0
            archive = internal_path(root, '.updates/downloads/' + release['asset']['name'])
            archive.parent.mkdir(parents=True, exist_ok=True)
        print('当前版本：' + current['version'])
        print('升级期间请保持服务停止；工具会自动备份数据库与配置，并保留上一版程序和依赖。')
        if not args.yes and input('开始升级？[y/N] ').strip().lower() != 'y':
            print('已取消，安装未作修改。')
            return 0
        if release:
            if archive.exists():
                archive.unlink()  # A bounded, task-owned incomplete download only.
            download_release(release, archive)
        installed, result = upgrade(root, archive, backup_module, Config, env_file,
                                    expected_version=release['version'] if release else None)
        print(f"升级完成：{installed}；更新 {result['changed']} 个文件，删除 {result['removed']} 个旧程序文件。")
        print('私有备份目录：' + result['backup'])
    if not args.no_start and not args.yes and input('现在启动酒馆？[Y/n] ').strip().lower() != 'n':
        return subprocess.call([str(active_python(root)), '-X', 'utf8', str(root / 'tools/start.py'), '--env-file', str(env_file)], cwd=root)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('已中断；如提示升级未完成，请再次运行更新工具恢复。') from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        # Provider/configuration/database errors may contain secrets. Never print
        # exception bodies, environment variables or SQL connection URLs.
        raise SystemExit(f'升级未完成（{type(error).__name__}）。请确认服务已停止、网络与依赖安装正常。若存在待恢复升级，再次运行本工具即可恢复。') from None
