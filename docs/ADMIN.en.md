# Administrator guide

[简体中文](ADMIN.md) | English · [Installation and model setup](DEPLOYMENT.en.md)

Sign in with the administrator account created by the setup wizard. Accounts created through normal registration are players; tavern membership does not grant platform administrator access. Interface labels below match the Chinese UI.

## Open your first tavern

1. Open **管理后台** (platform administration) in the top bar. In **开放新酒馆**, enter a display name and a URL slug. The slug uses lowercase letters, digits and hyphens, such as `abyss-tavern`.
2. Select **深渊酒馆**. Enable **公开展示** to show it to signed-in players. Also enable **允许玩家自由加入** if they should be able to join themselves, then click **开放酒馆**.
3. Return home and enter the tavern. The first visit initializes its game data. For a private tavern, ask friends to register, then add them under **分配成员**.
4. Check the home page with a player account. A private tavern is invisible to players who have not been assigned membership.

Each tavern has separate characters, items and stories. Platform administration controls access, memberships, accounts and game processes. The **管理** tab inside a tavern controls that tavern's gameplay settings.

## Admit your first players

A fresh tavern has no adventurers. Players submit character reservations from **我的**. Administrators open **建筑 → 预约管理**, check waiting reservations and available beds, enter a draw count and click **执行抽取**. A successful draw admits the player's adventurer. If there are no free beds, address dormitory capacity first.

Reservation management offers **宿舍完工后自动抽取** (draw after dormitory construction) and **每天 20:00 自动抽取** (daily draw at 20:00 Beijing time). Choose these according to your opening schedule. An empty adventurer list in a new tavern does not mean installation failed.

## Configure models and game pace

As an administrator, enter the target tavern and open **管理 → 探险 → 运行与 AI → S1 AI 模型配置**.

1. Enter a profile name, provider protocol, API base URL, model ID and API key. You may add multiple profiles.
2. Under **AI 用途分配**, assign profiles and reasoning levels to story generation, outlines, reviews, buildings and appeals. Search features to assign several at once.
3. Click **保存模型与用途配置**. Subsequent requests use the saved settings immediately; no terminal command or restart is needed. Requests already in progress retain their original settings.

Keys are write-only. Leave an existing key blank to retain it; select its clear option to remove it. Expand **备用线路与备用 Key** to choose a full fallback profile or configure additional credentials for the same endpoint.

Each tavern stores its own settings in its own database, retains them across restarts, and does not change other taverns. Before the first independent save, a tavern displays the initial deployment defaults. Stale edits are rejected: refresh and review before saving again.

The same panel provides **AI 用量统计与失败详情** (usage statistics and failure details). After topping up a credential paused for insufficient balance, use **充值后恢复调用**. This button does not make an AI request.

For Chinese story prose, our only recommended model is **`deepseek-v4-pro-0423`**. In the project's playtesting, newer versions produced poorer Chinese narrative results. Pin this version.

Inside the tavern, open **管理 → 探险 → 运行与 AI** to change party sizes, departure intervals, daily expedition quotas and fast/slow periods, then save. Times in this panel use Beijing time. Start with the defaults and adjust after observing your game's pace.

The **AI master switch at the top of the management panel** controls new AI requests. Scheduled AI blackout periods are configured under **运行与 AI**. Pausing AI does not stop the platform or immediately cancel requests already sent to a provider. Wait for ongoing stories and settlements before maintenance.

## Common administration tasks

| Task | Location |
| --- | --- |
| Public visibility, self-joining and membership | Platform **管理后台** |
| Notices and level caps | Tavern **管理 → 运营** |
| Matchmaking, dispatch and game pace | Tavern **管理 → 探险** |
| Player data appeals | Tavern **管理 → 审核 → 数据申诉** |
| Buildings, reservations, prize wheel and item catalog | Administrator cards under **建筑**, or **管理 → 建筑图鉴** |

Only administrators see these tools. Check the tavern name before changing settings, especially when hosting multiple taverns.

## Before inviting players

- Verify joining, character reservation and admission with a player account.
- Confirm your model configuration, AI switch and dispatch settings. Actual story generation consumes provider credits.
- Share a reachable server address. `127.0.0.1` only reaches the visitor's own computer. Follow the [deployment guide](DEPLOYMENT.en.md) for HTTPS hosting, offline backups and restoration.

## AI input compression

In the target tavern, open **管理 → 探险 → 运行与 AI → S1 AI 模型配置** and use **资产描述压缩与特质分类筛选** (asset description compression and trait context filtering). This switch is **off by default**, applies only to the current tavern, and survives restarts.

- **Off:** applicable trait, skill, and equipment descriptions are sent in full, avoiding omissions caused by summarization or context filtering. Longer contexts can make the model overlook or forget details, dilute its attention, and consume more tokens at a higher cost.
- **On:** reviewed summaries and trait context filtering reduce input length and token costs, with a risk of omitted information. Generating summaries and classifications also requires AI calls.

Equipment, archived assets, and asset dependency rules still apply. Changes affect newly prepared inputs. Frozen expedition inputs and their reviews remain consistent; requests already sent are not canceled. Turning compression off stops new background asset compression tasks and prevents old summaries from being used in new inputs.
