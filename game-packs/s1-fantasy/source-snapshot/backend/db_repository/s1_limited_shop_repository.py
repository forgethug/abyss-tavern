"""限时商店查询与短事务锁；商品库存由既有 ShopItem 统一持有。"""
from sqlalchemy.orm import noload
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1BuildingTemplate as Template, AdventurerS1BuildingInstance as Instance,
    AdventurerS1Item as Item, AdventurerS1ShopItem as Listing,
)


class LimitedShopRepository:
    @staticmethod
    def list_page(page, per_page):
        query = Template.query.options(noload(Template.instances)).filter_by(handler_key='limited_shop')
        total = query.count()
        return query.order_by(Template.id.desc()).offset((page - 1) * per_page).limit(per_page).all(), total

    @staticmethod
    def get(template_id, lock=False):
        query = Template.query.options(noload(Template.instances)).filter_by(id=template_id, handler_key='limited_shop')
        if lock:
            query = query.populate_existing().with_for_update()
        return query.one_or_none()

    @staticmethod
    def catalog_by_ids(ids):
        return {row.id: row for row in Item.query.filter(Item.id.in_(ids)).all()}

    @staticmethod
    def catalog_by_names(names):
        return {row.name: row for row in Item.query.filter(Item.name.in_(names)).all()}

    @staticmethod
    def listings(item_ids, lock=False):
        query = Listing.query.filter(Listing.item_id.in_(item_ids)).order_by(Listing.id)
        if lock:
            query = query.populate_existing().with_for_update()
        return {row.item_id: row for row in query.all()}

    @staticmethod
    def create_template(code, name, description):
        if Template.query.filter((Template.code == code) | (Template.name == name)).first():
            raise ValueError('商店标识或名称已存在，请刷新列表后编辑')
        row = Template(code=code, name=name, description=description, icon='🥮',
                       handler_key='limited_shop', category='leisure', building_type='unique',
                       build_hours=0, helper_slots=0, required_materials=None)
        db.session.add(row)
        db.session.flush()
        db.session.add(Instance(template_id=row.id, status='built'))
        return row

    @staticmethod
    def create_items(data):
        rows = [Item(**entry) for entry in data]
        db.session.add_all(rows)
        db.session.flush()
        return {row.name: row for row in rows}

    @staticmethod
    def create_listing(item_id, price, stock):
        row = Listing(item_id=item_id, price=price, stock=stock)
        db.session.add(row)
        return row
