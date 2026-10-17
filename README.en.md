# Abyss Tavern

[简体中文](README.md) | English

**AI storytelling × idle adventures × character progression**

Prepare for the next expedition at the tavern, then discover the story your adventurer brings home.

Abyss Tavern is an AI-powered fantasy text RPG. Create an adventurer, choose equipment and skills, and explore dungeons with companions. The AI draws on the party's abilities, personalities, history and surroundings to narrate battles, encounters and discoveries.

An expedition may bring new equipment, skills and traits—or injuries and losses. Your character's progress and experiences carry forward into future adventures.

The game has been tested and refined over several months with hundreds of players, improving the stories, character progression and tavern activities.

## What can you play?

- **Party expeditions:** prepare supplies, choose a destination and set out. Expeditions progress automatically; read your party's story as it unfolds.
- **Character progression:** develop a class, learn skills, gain traits and experiment with equipment and ability combinations.
- **Dungeons and encounters:** explore different dungeons, discover hidden areas and face dangerous enemies.
- **Special opportunities:** seek class progression at the Class Trial Grounds, learn skills in the Enlightenment Realm, or seek treatment at the Calamity Relief Clinic.
- **Tavern activities:** learn from mentors, combine materials in the construct workshop, cook, rest and recover.
- **Items and trading:** collect loot, manage storage and trade equipment and materials on the market.
- **Commissions and conversation:** complete commissions, interact at the tavern and share discoveries in expedition-log comments.

The game revolves around preparation before departure and character development after returning. Expeditions can lead to rewards, injuries, losses or death.

## Host your own tavern

Play alone or invite friends. You can create multiple independent taverns, each with its own characters, items and world records.

**The game interface and setup prompts are currently in Chinese.** This English documentation explains the game and walks you through installation, including translations of the setup prompts.

## Recommended model

**For Chinese story prose, our only recommended model is `deepseek-v4-pro-0423`.** In the project's playtesting, newer versions produced poorer Chinese narrative results. Pin this exact version rather than automatically switching to the latest model.

## Get started

Install **Python 3.12+** and **Node.js 22.12+ or 24 LTS**, then download and extract the source:

- **Windows:** double-click `tools/setup.cmd`.
- **Linux:** run `python3 tools/setup.py` from the project directory.
- Enter your API base URL, model ID and API key, then create an administrator account. Once the server starts, open `http://127.0.0.1:5500`.
- For later starts, use `tools/start.cmd` on Windows or `python3 tools/start.py` on Linux.

See the [deployment and model setup guide](docs/DEPLOYMENT.en.md) and the [playing guide](game-packs/s1-fantasy/PLAYING.en.md).

The [administrator guide](docs/ADMIN.en.md) covers opening a tavern, admitting players, configuring AI and game pace, and finding management tools.

## License

The code is available under the [MIT License](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for source and dependency information.
