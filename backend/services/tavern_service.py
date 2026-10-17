from runtime_core.time import utc_now
import re
import uuid
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from models import Tavern, TavernAccessPolicy, TavernMembership, User
from services.game_pack_registry import (
    GamePackConfigurationError,
    GamePackRegistry,
)


class TavernService:
    VALID_STATUSES = {"draft", "active", "disabled"}
    VALID_MEMBER_ROLES = {"owner", "manager", "member"}
    VALID_RUNTIME_ACTIONS = {"start", "stop", "restart"}

    @staticmethod
    def get_by_id(tavern_id: int) -> Optional[Tavern]:
        return db_manager.query(Tavern).filter(
            Tavern.id == tavern_id,
            Tavern.game_pack.in_(tuple(GamePackRegistry.all())),
        ).first()

    @staticmethod
    def resolve_access(
        user: User,
        slug: str,
    ) -> Tuple[Optional[Tuple[Tavern, Optional[TavernMembership]]], Optional[str]]:
        tavern = db_manager.query(Tavern).filter_by(slug=slug).first()
        if not tavern or tavern.status != "active" or tavern.game_pack not in GamePackRegistry.all():
            return None, "酒馆不存在或未开放"

        membership = (
            db_manager.query(TavernMembership)
            .filter_by(tavern_id=tavern.id, user_id=user.id, status="active")
            .first()
        )
        if not membership and not user.is_admin:
            return None, "你不是这个酒馆的成员"
        return (tavern, membership), None

    @staticmethod
    def create_tavern(
        creator: User,
        display_name: str,
        slug: str,
        game_pack: Optional[str] = None,
        is_public: bool = False,
        allow_self_join: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        display_name = (display_name or "").strip()
        slug = (slug or "").strip().lower()
        if type(is_public) is not bool or type(allow_self_join) is not bool:
            return None, "公开与自由加入设置必须是布尔值"
        if allow_self_join and not is_public:
            return None, "允许自由加入前必须先公开展示"
        if not display_name or len(display_name) > current_app.config["TAVERN_NAME_MAX_LENGTH"]:
            return None, "酒馆名称不能为空且不能超过长度限制"
        if len(slug) > current_app.config["TAVERN_SLUG_MAX_LENGTH"] or not re.fullmatch(
            current_app.config["TAVERN_SLUG_PATTERN"],
            slug,
        ):
            return None, "访问标识只能使用小写字母、数字和单个连字符"
        if db_manager.query(Tavern).filter_by(slug=slug).first():
            return None, "访问标识已存在"
        selected_game_pack = game_pack or current_app.config["DEFAULT_GAME_PACK"]
        try:
            pack_definition = GamePackRegistry.get(selected_game_pack)
        except GamePackConfigurationError:
            return None, "游戏包不存在或与当前平台不兼容"

        tavern = Tavern(
            slug=slug,
            display_name=display_name,
            status="active",
            game_pack=pack_definition.pack_id,
            game_pack_release=pack_definition.pack_version,
            runtime_mode=current_app.config["DEFAULT_RUNTIME_MODE"],
            runtime_namespace=str(uuid.uuid4()),
            created_by_user_id=creator.id,
        )
        ok, error = db_manager.add(tavern)
        if not ok:
            return None, error
        ok, error = db_manager.flush()
        if not ok:
            return None, error
        tavern.runtime_port = current_app.config["RUNTIME_BASE_PORT"] + tavern.id

        membership = TavernMembership(
            tavern_id=tavern.id,
            user_id=creator.id,
            role="owner",
            status="active",
        )
        ok, error = db_manager.add(membership)
        if not ok:
            return None, error
        policy = TavernAccessPolicy(
            tavern_id=tavern.id,
            is_public=is_public,
            allow_self_join=allow_self_join,
        )
        ok, error = db_manager.add(policy)
        if not ok:
            return None, error
        ok, error = db_manager.commit()
        if not ok:
            if "unique" in (error or "").lower():
                return None, "访问标识已存在"
            return None, "创建酒馆失败"
        return tavern.to_admin_dict(), None

    @staticmethod
    def list_for_user(user: User) -> list:
        installed_packs = tuple(GamePackRegistry.all())
        if user.is_admin:
            taverns = db_manager.query(Tavern).filter(Tavern.game_pack.in_(installed_packs)).order_by(Tavern.id.asc()).all()
            result = []
            for tavern in taverns:
                item = tavern.to_admin_dict()
                item["membership_role"] = "platform_admin"
                result.append(item)
            return result

        membership_rows = (
            db_manager.query(TavernMembership)
            .join(Tavern)
            .filter(
                TavernMembership.user_id == user.id,
                TavernMembership.status == "active",
                Tavern.status == "active",
                Tavern.game_pack.in_(installed_packs),
            )
            .order_by(Tavern.id.asc())
            .all()
        )
        membership_by_tavern = {
            row.tavern_id: row
            for row in membership_rows
        }
        public_taverns = (
            db_manager.query(Tavern)
            .join(TavernAccessPolicy)
            .filter(
                Tavern.status == "active",
                TavernAccessPolicy.is_public.is_(True),
                Tavern.game_pack.in_(installed_packs),
            )
            .order_by(Tavern.id.asc())
            .all()
        )
        taverns_by_id = {
            row.tavern.id: row.tavern
            for row in membership_rows
        }
        taverns_by_id.update({tavern.id: tavern for tavern in public_taverns})
        return [
            tavern.to_dict(
                role=(
                    membership_by_tavern[tavern.id].role
                    if tavern.id in membership_by_tavern
                    else None
                ),
                can_self_join=bool(
                    tavern.id not in membership_by_tavern
                    and tavern.access_policy
                    and tavern.access_policy.is_public
                    and tavern.access_policy.allow_self_join
                ),
            )
            for tavern in sorted(taverns_by_id.values(), key=lambda row: row.id)
        ]

    @staticmethod
    def join_public(
        user: User,
        slug: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        tavern = (
            db_manager.query(Tavern)
            .filter_by(slug=slug, status="active")
            .first()
        )
        if tavern is None or tavern.game_pack not in GamePackRegistry.all():
            return None, "酒馆不存在或未开放"
        policy = tavern.access_policy
        if (
            policy is None
            or not policy.is_public
            or not policy.allow_self_join
        ):
            return None, "这个酒馆未开放自由加入"
        membership = (
            db_manager.query(TavernMembership)
            .filter_by(tavern_id=tavern.id, user_id=user.id)
            .first()
        )
        if membership is None:
            membership = TavernMembership(
                tavern_id=tavern.id,
                user_id=user.id,
                role="member",
                status="active",
            )
            ok, error = db_manager.add(membership)
            if not ok:
                return None, error
        else:
            membership.role = (
                membership.role
                if membership.role in TavernService.VALID_MEMBER_ROLES
                else "member"
            )
            membership.status = "active"
        ok, error = db_manager.commit()
        if not ok:
            return None, error
        return tavern.to_dict(role=membership.role), None

    @staticmethod
    def get_runtime_context(user: User, slug: str) -> Tuple[Optional[dict], Optional[str]]:
        access, error = TavernService.resolve_access(user, slug)
        if error:
            return None, error
        tavern, membership = access
        try:
            pack_definition = GamePackRegistry.require_release(
                tavern.game_pack,
                tavern.game_pack_release,
            )
        except GamePackConfigurationError:
            return None, "酒馆固定的游戏包版本未安装或与当前平台不兼容"

        runtime_status = tavern.runtime_status
        if current_app.config["RUNTIME_AUTO_START"]:
            from services.runtime_supervisor_service import RuntimeSupervisorService

            ready, runtime_error = RuntimeSupervisorService.ensure_ready(tavern, user)
            if not ready:
                return None, runtime_error or "酒馆 Runtime 启动失败"
            runtime_status = "ready"

        return {
            "tavern": tavern.to_dict(role=membership.role if membership else "platform_admin"),
            "runtime": {
                "game_pack": tavern.game_pack,
                "release": tavern.game_pack_release,
                "runtime_api_version": pack_definition.runtime_api_version,
                "frontend_plugin": pack_definition.frontend_plugin,
                "entry_path": f"/play/{tavern.slug}",
                "migration_status": runtime_status,
            },
        }, None

    @staticmethod
    def add_member(
        tavern_id: int,
        user_id: int,
        role: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if role not in TavernService.VALID_MEMBER_ROLES:
            return None, "成员角色无效"
        tavern = db_manager.query(Tavern).filter_by(id=tavern_id).first()
        user = db_manager.query(User).filter_by(id=user_id, is_active=True).first()
        if not tavern:
            return None, "酒馆不存在"
        if not user:
            return None, "用户不存在或已停用"

        membership = (
            db_manager.query(TavernMembership)
            .filter_by(tavern_id=tavern_id, user_id=user_id)
            .first()
        )
        if membership:
            membership.role = role
            membership.status = "active"
        else:
            membership = TavernMembership(
                tavern_id=tavern_id,
                user_id=user_id,
                role=role,
                status="active",
            )
            ok, error = db_manager.add(membership)
            if not ok:
                return None, error

        ok, error = db_manager.commit()
        if not ok:
            return None, error
        return membership.to_dict(), None

    @staticmethod
    def control_runtime(
        actor: User,
        tavern_id: int,
        action: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if action not in TavernService.VALID_RUNTIME_ACTIONS:
            return None, "Runtime 操作无效"

        tavern = db_manager.query(Tavern).filter_by(id=tavern_id).first()
        if not tavern:
            return None, "酒馆不存在"
        if action in {"start", "restart"} and tavern.status != "active":
            return None, "请先重新开放酒馆"
        if action in {"start", "restart"} and tavern.game_pack not in GamePackRegistry.all():
            return None, "游戏包不存在或与当前平台不兼容"

        from services.runtime_supervisor_service import RuntimeSupervisorService

        if action == "start":
            ok, error = RuntimeSupervisorService.ensure_ready(tavern, actor)
        elif action == "stop":
            ok, error = RuntimeSupervisorService.stop_runtime(tavern)
        else:
            ok, error = RuntimeSupervisorService.restart_runtime(tavern, actor)

        if not ok:
            return None, error or "Runtime 操作失败"
        return tavern.to_admin_dict(), None

    @staticmethod
    def update_tavern(
        tavern_id: int,
        display_name: Optional[str],
        status: Optional[str],
        is_public: Optional[bool] = None,
        allow_self_join: Optional[bool] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        tavern = db_manager.query(Tavern).filter_by(id=tavern_id).first()
        if not tavern:
            return None, "酒馆不存在"
        if is_public is not None and type(is_public) is not bool:
            return None, "公开展示设置必须是布尔值"
        if allow_self_join is not None and type(allow_self_join) is not bool:
            return None, "自由加入设置必须是布尔值"
        if display_name is not None:
            display_name = display_name.strip()
            if not display_name or len(display_name) > current_app.config["TAVERN_NAME_MAX_LENGTH"]:
                return None, "酒馆名称无效"
            tavern.display_name = display_name
        if status is not None:
            if status not in TavernService.VALID_STATUSES:
                return None, "酒馆状态无效"
            if status == "disabled" and tavern.status != "disabled":
                from services.runtime_supervisor_service import RuntimeSupervisorService

                stopped, error = RuntimeSupervisorService.stop_runtime(tavern)
                if not stopped:
                    return None, error or "Runtime 停止失败"
            tavern.status = status
        if is_public is not None or allow_self_join is not None:
            policy = tavern.access_policy
            if policy is None:
                policy = TavernAccessPolicy(tavern_id=tavern.id)
                ok, error = db_manager.add(policy)
                if not ok:
                    return None, error
            effective_public = (
                is_public
                if is_public is not None
                else bool(policy.is_public)
            )
            effective_self_join = (
                allow_self_join
                if allow_self_join is not None
                else bool(policy.allow_self_join)
            )
            if allow_self_join is True and not effective_public:
                return None, "允许自由加入前必须先公开展示"
            if not effective_public:
                effective_self_join = False
            policy.is_public = effective_public
            policy.allow_self_join = effective_self_join
        ok, error = db_manager.commit()
        if not ok:
            return None, error
        return tavern.to_admin_dict(), None

    @staticmethod
    def set_user_active(
        actor: User,
        user_id: int,
        is_active: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        user = db_manager.query(User).filter_by(id=user_id).first()
        if not user:
            return None, "用户不存在"
        if user.id == actor.id and not is_active:
            return None, "不能停用自己的管理员账号"
        user.is_active = is_active
        if not is_active:
            user.token_invalidated_at = utc_now()
        ok, error = db_manager.commit()
        if not ok:
            return None, error
        return user.to_dict(), None
