"""Load optional panels when their existing v-if branch mounts them."""
import re


VIEW_COMPONENTS = {
    'CreateAdventurerPanel', 'ArchiveAppealPanel', 'AdventurerDetailModal',
    'ExpeditionLogModal', 'AdventurerLogsModal', 'BuildingDetailModal',
    'DormRoomModal', 'MarketPanel', 'SecretAuctionPanel', 'TeamLootPanel',
    'TaskBoardPanel', 'CampaignHallPanel', 'CommissionBoardPanel',
    'DataAppealModal', 'TavernReliefModal', 'AppealProjectsModal',
    'TitlePreviewModal', 'CodenameTextCorrectionModal',
    'CompanionEvolutionLedgerModal', 'SquadModal', 'StarlightCeremonyHall',
}


def project_lazy_ui(relative: str, raw: bytes) -> bytes:
    if not relative.endswith(('/views/AdventurerS1View.vue', '/components/AdminHubPanel.vue')):
        return raw
    source = raw.decode('utf-8-sig').replace('\r\n', '\n')
    is_view = relative.endswith('/views/AdventurerS1View.vue')
    found = set()

    def lazy(match):
        name, path = match.groups()
        if is_view and name not in VIEW_COMPONENTS:
            return match.group(0)
        found.add(name)
        return f"const {name} = defineAsyncComponent(() => import('{path}'))"

    source = re.sub(r"^import (\w+) from '([^']+\.vue)'$", lazy, source, flags=re.M)
    if is_view and found != VIEW_COMPONENTS:
        raise ValueError(f'Lazy UI source drift: {VIEW_COMPONENTS - found}')
    if not is_view:
        source = source.replace('import { computed,', 'import { defineAsyncComponent, computed,', 1)
    return source.encode('utf-8')
