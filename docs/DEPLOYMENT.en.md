# Install and host Abyss Tavern

[简体中文](DEPLOYMENT.md) | English

## First installation

Install **Python 3.12+** and **Node.js 22.12+ or 24 LTS** (including npm). On Windows, add Python to PATH during installation and open a new terminal afterward. On Linux, install your distribution's matching Python venv package if required.

Download the source using **Code → Download ZIP** on GitHub and extract it completely, or clone the repository with Git. Run scripts from the extracted project, not from inside the ZIP.

**Windows:** double-click `tools/setup.cmd`, or run this from the project directory:

```powershell
python tools/setup.py
```

**Linux:**

```bash
python3 tools/setup.py
```

The wizard installs dependencies, builds the website, generates local configuration, asks for model settings, creates the first administrator and starts the game. The first run needs internet access to download dependencies. Python and Node.js must already be installed.

## Enter your model settings

**For Chinese story prose, our only recommended model is `deepseek-v4-pro-0423`.** In the project's playtesting, newer versions produced poorer Chinese narrative results. Pin this exact version rather than automatically switching to the latest model.

Use an **OpenAI Chat Completions-compatible API**. A chat website subscription does not necessarily include API access. Obtain the endpoint, model ID and key from your provider.

The setup prompts are in Chinese:

| Prompt | What to enter |
| --- | --- |
| `接口基础地址` | Your provider's API base URL, such as `https://your-provider.example/v1`. Use the actual provider address, not this example or a chat website. A trailing `/chat/completions` is removed automatically. |
| `模型 ID` | The exact model identifier from the provider's API model list. |
| `API Key` | Your API key. Input is hidden; paste it and press Enter. Do not add `Bearer ` or enter your account password. |
| `是否修改模型配置？[y/N]` | Existing settings were found. Press Enter to keep them, or type `y` to edit the first model profile. Blank fields during editing keep their previous values. |

Choose a model with conversational and structured JSON output support and sufficient context capacity. Story quality, speed and cost depend on the provider and model. Setup saves configuration without sending a test generation request.

Settings are stored in your local `.env`. Keep this file private. Other model profiles and their assignments are preserved when you edit the first profile.

## Create your administrator and play

See the [administrator guide](ADMIN.en.md) for opening a tavern, admitting adventurers and using in-game management tools.

At `管理员用户名`, enter a username of 3–32 letters, digits or underscores. At `管理员密码`, enter a password of 6–64 characters, then enter it again when asked to confirm. Password input is hidden. If an active administrator already exists, setup keeps that account and password.

After the startup message appears, open **http://127.0.0.1:5500** and sign in. Create a tavern in the platform administration page and enter it. Tavern settings control whether it appears publicly and whether players can join themselves; administrators can also assign members.

The game interface is currently in Chinese. Keep the server terminal open while playing. Press **Ctrl+C** and wait for it to exit when stopping.

## Start again or change models

For normal startup, double-click `tools/start.cmd` on Windows or run `python3 tools/start.py` on Linux. You do not need to reinstall.

To change models, keys or endpoints, sign in as an administrator and open the target tavern’s **管理 → 探险 → 运行与 AI → S1 AI 模型配置** panel. Add profiles and assign them to features, then save. Changes apply only to that tavern and take effect for subsequent requests without a restart. Blank key fields retain existing credentials. See the [administrator guide](ADMIN.en.md).

The installer’s `.env` settings provide initial defaults. Independent web settings are stored in each tavern’s database; include the databases in backups.

If installation was interrupted after dependencies were installed, use the virtual environment's Python to run `tools/setup.py --configure-only`. Add `--no-start` to configure without starting. For an external private configuration file, use `--env-file <path>` during both setup and startup, or set `PLATFORM_ENV_FILE` consistently.

## Common problems

| Problem | Check |
| --- | --- |
| Python, Node.js or npm cannot be found | Install the required versions, check PATH, then open a new terminal. |
| Dependency download failed | Check connectivity, resolve the reported download error and rerun setup. |
| API returns 401 or 403 | Check the key, endpoint, model permissions and provider account status. |
| Model not found or API returns 404 | Verify the exact model ID and API base URL. |
| Page will not open | Check that the server is running, the build succeeded and the port is available. Use the address printed at startup. |
| Model changes have no effect | Stop and restart the entire platform; check that it uses the configuration file you edited. |
| Friends cannot open your localhost address | `127.0.0.1` refers to their own computer. Configure a reachable server and HTTPS as described below. |

## Hosting for other players

The supported server entry point, `backend/serve.py`, serves both the website and API at `127.0.0.1:5500` by default. Configure your domain in `deploy/Caddyfile.example`, point DNS to your server and expose HTTPS through Caddy. Keep runtime ports private. Use one platform server process; each tavern has its own runtime process and database.

For Linux services, adapt `deploy/ai-rpg.service.example` to your service account, paths and private configuration. Keep `KillMode=control-group` so child processes are cleaned up when the service stops.

SQLite is the default for a local setup. For long-running servers, use separate PostgreSQL databases for the platform and each tavern. Configure `DATABASE_URL` and `RUNTIME_DATABASE_URL_TEMPLATE`; supported template fields are `{runtime_id}`, `{runtime_key}`, `{tavern_id}`, `{slug}` and `{pack_id}`. Create the databases beforehand. Each tavern must resolve to a distinct database. Use absolute paths for custom SQLite URLs.

## Back up your game

Wait for ongoing adventures and settlements to finish, stop the taverns from administration, then stop the platform. Using the same private configuration and virtual environment as the server, run:

```powershell
# Windows
.\.venv\Scripts\python.exe tools/backup.py backup D:\private-backups\tavern
```

```bash
# Linux
.venv/bin/python tools/backup.py backup /private-backups/tavern
```

Save `.env` and the corresponding source version separately. Backups contain player and account data; keep them private.

To restore, configure a new destination with the same database type, then run `tools/backup.py restore <backup-directory>` using the virtual environment's Python. SQLite destination files must not exist; PostgreSQL destination databases must exist and be empty. The tool validates file checksums before restoration and refuses to overwrite existing data. PostgreSQL backup and restore require compatible `pg_dump` and `pg_restore` tools in PATH, or a `PG_BIN` directory setting.

## Update the game

Prefer the `abyss-tavern-vVERSION.zip` asset from [Releases](https://github.com/w7lau/abyss-tavern/releases) for new installations. It includes the upgrade manifest. If no release exists yet, use the source download or Git clone.

Wait for adventures and settlements to finish, stop the taverns in administration, then stop the platform normally. **Windows: double-click `tools/update.cmd`.** Linux: run `python3 tools/update.py`. Confirm the update; the tool backs up data, installs dependencies, builds the frontend and updates the program. Interactive runs offer to start the game afterward.

If your installation has no updater, extract a formal release into a **separate folder**, run its updater and enter the original installation directory when prompted. Do not manually overwrite a running installation.

Use `--check` for a read-only version check. Use the same `--env-file <path>` or `PLATFORM_ENV_FILE` as startup. `--yes --no-start` supports unattended maintenance after the service has been stopped. No published package is a normal “no update” result.

The updater preserves `.env`, accounts, player data and each tavern's independent model profiles, keys and feature assignments. It downloads the complete ZIP, verifies SHA-256, then replaces only changed program files and removes obsolete managed files. Local source edits or conflicting custom files stop the update. Development branches are not followed automatically.

Private backups, recovery records and the active Python environment live under `.updates/transactions/`. **Do not publish or delete the entire `.updates` directory.** Allow disk space for backups, dependencies and builds; dependency installation requires network access.

Candidate startup checks use disposable copies and make no model calls. SQLite checks use platform and tavern backups. PostgreSQL uses `pg_dump` backups and a fresh SQLite startup probe; this does not validate PostgreSQL data migrations. Automatic upgrades currently accept compatible releases with the same data format only.

Dependency, build or startup-check failures leave the original program intact. File replacement failures restore the previous program automatically. After a power loss or forced interruption, rerun the updater to recover before starting. If the installed tool cannot run, use a freshly extracted release's updater with the original installation directory. Never remove a pending marker to bypass recovery.

Existing startup scripts and service entrypoints select the updated dependency environment. Do not mix the updater and `git pull` in one installation. Maintain a separate deployment process for customized source.

To downgrade after successful updates and further gameplay, stop the service and restore the matching old source, configuration and database backup into empty destinations. This loses progress made after that backup; the updater does not perform automatic data downgrades.

### Publish an update

Maintainers synchronize tested public source, update `VERSION`, then push its matching `vMAJOR.MINOR.PATCH` tag. GitHub Actions runs Windows/Linux verification before publishing the source ZIP, file manifest and SHA-256. Packages exclude private configuration, databases and Git history. Upstream changes must be adapted and tested before publication.

## AI input compression

In the target tavern, open **管理 → 探险 → 运行与 AI → S1 AI 模型配置** and use **资产描述压缩与特质分类筛选** (asset description compression and trait context filtering). This switch is **off by default**, applies only to the current tavern, and survives restarts.

- **Off:** applicable trait, skill, and equipment descriptions are sent in full, avoiding omissions caused by summarization or context filtering. Longer contexts can make the model overlook or forget details, dilute its attention, and consume more tokens at a higher cost.
- **On:** reviewed summaries and trait context filtering reduce input length and token costs, with a risk of omitted information. Generating summaries and classifications also requires AI calls.

Equipment, archived assets, and asset dependency rules still apply. Changes affect newly prepared inputs. Frozen expedition inputs and their reviews remain consistent; requests already sent are not canceled. Turning compression off stops new background asset compression tasks and prevents old summaries from being used in new inputs.
