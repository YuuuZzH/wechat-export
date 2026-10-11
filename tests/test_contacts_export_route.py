"""`/api/address-book/export` 与 `/api/address-book` 的 HTTP 层回归。

背景：通讯录导出此前是路由里内联的一段 CSV 拼装，硬编码、无格式参数、
无 BOM（Excel 打开中文乱码）、也不支持把界面上的筛选条件带进导出。
本次改为复用 `contacts_export`，并保持旧行为的向后兼容。

同时锁住一个顺带修掉的隐患：旧路由对 `sort` 是**就地排序**，
而 `get_all_contacts()` 返回的是进程内共享缓存列表 ——
一次 `?sort=msg_count` 请求会把缓存顺序改掉，泄漏到后续所有请求。
"""
import csv
import io

import pytest

from web.app import create_app
from web.routes import api as api_module


def _contact(**kw):
    base = {
        'wxid': 'wxid_x', 'display_name': '某人', 'remark': '', 'nick_name': '',
        'alias': '', 'avatar_url': '/api/avatar/wxid_x', 'msg_count': 0,
        'last_msg_time': None, 'is_group': False, 'labels': [],
    }
    base.update(kw)
    return base


@pytest.fixture
def contacts():
    """函数内重新构造，避免用例之间通过共享列表互相污染。"""
    return [
        _contact(wxid='wxid_alpha', display_name='李老师', remark='李老师',
                 alias='lisi', phone='13800000001', phone_source='contact_db',
                 sex=2, country='CN', province='Shaanxi', city="Xi'an",
                 signature='签名A', msg_count=1200, last_msg_time=1789531971,
                 labels=['only_work'], label_ids=[5]),
        _contact(wxid='wxid_beta', display_name='<script>alert(1)</script>',
                 remark='XSS', msg_count=0, labels=['non_work', 'only_work'],
                 label_ids=[4, 5]),
        _contact(wxid='12345678@chatroom', display_name='相亲相爱一家人',
                 is_group=True, msg_count=99, last_msg_time=1789600000,
                 labels=['家人'], label_ids=[6]),
    ]


@pytest.fixture
def client(tmp_path, monkeypatch, contacts):
    d = tmp_path / 'decrypted'
    d.mkdir()
    app = create_app(str(d), wxid='wxid_example12345')
    app.config['TESTING'] = True
    # get_all_contacts 是 api 模块级的名字，patch 它即可替换数据源
    monkeypatch.setattr(api_module, 'get_all_contacts', lambda _dir: contacts)
    return app.test_client()


# --------------------------------------------------------------------------
# 列表接口（回归：不得因导出改动而变脸）
# --------------------------------------------------------------------------

class TestListEndpointUnchanged:
    def test_shape(self, client):
        r = client.get('/api/address-book')
        assert r.status_code == 200
        body = r.get_json()
        assert set(['contacts', 'total', 'page', 'per_page', 'total_pages']) <= set(body)
        assert body['total'] == 3

    def test_sort_does_not_mutate_shared_cache(self, client):
        """旧实现就地排序会污染 get_all_contacts() 的缓存。"""
        client.get('/api/address-book?sort=msg_count')
        after = client.get('/api/address-book').get_json()['contacts']
        names = [c['display_name'].lower() for c in after]
        assert names == sorted(names), '列表顺序被 sort 请求泄漏污染了'

    def test_kind_filter_available(self, client):
        body = client.get('/api/address-book?kind=groups').get_json()
        assert body['total'] == 1
        assert body['contacts'][0]['is_group'] is True

    def test_kind_contacts_excludes_groups(self, client):
        body = client.get('/api/address-book?kind=contacts').get_json()
        assert body['total'] == 2
        assert all(not c['is_group'] for c in body['contacts'])

    def test_label_filter(self, client):
        body = client.get('/api/address-book?label=only_work').get_json()
        assert body['total'] == 2

    def test_contact_payload_carries_new_fields(self, client):
        """手机号 / 性别 / 签名 / 地区 / 标签 要出现在 JSON 里，界面才能显示。"""
        rows = client.get('/api/address-book').get_json()['contacts']
        got = [c for c in rows if c['wxid'] == 'wxid_alpha'][0]
        assert got['phone'] == '13800000001'
        assert got['sex'] == 2
        assert got['signature'] == '签名A'
        assert got['country'] == 'CN'
        assert got['labels'] == ['only_work']

    def test_labels_endpoint_lists_distinct_labels(self, client):
        body = client.get('/api/address-book/labels').get_json()
        # 中文标签排在 ASCII 之后
        assert body['labels'] == ['non_work', 'only_work', '家人']

    def test_labels_endpoint_unions_db_labels(self, tmp_path, monkeypatch):
        """contact_label 里定义了、但当前没有联系人使用的标签也要能选到（便于发现）。"""
        import sqlite3
        d = tmp_path / 'lbl'
        (d / 'contact').mkdir(parents=True)
        con = sqlite3.connect(str(d / 'contact' / 'contact.db'))
        con.execute('CREATE TABLE contact_label (label_id_ INTEGER,'
                    ' label_name_ TEXT, sort_order_ INTEGER)')
        con.executemany('INSERT INTO contact_label VALUES (?,?,?)',
                        [(5, 'only_work', 0), (7, '尚未使用', 1)])
        con.commit()
        con.close()
        app = create_app(str(d), wxid='wxid_example12345')
        app.config['TESTING'] = True
        monkeypatch.setattr(api_module, 'get_all_contacts',
                            lambda _dir: [_contact(labels=['only_work'])])
        c = app.test_client()
        assert c.get('/api/address-book/labels').get_json()['labels'] == \
            ['only_work', '尚未使用']


# --------------------------------------------------------------------------
# 导出接口
# --------------------------------------------------------------------------

class TestExportFormats:
    def test_csv_default_is_backward_compatible(self, client):
        """不传 format 时必须仍是 CSV —— 老链接不能坏。"""
        r = client.get('/api/address-book/export')
        assert r.status_code == 200
        assert r.mimetype == 'text/csv'
        assert r.data.startswith(b'\xef\xbb\xbf')

    def test_explicit_csv(self, client):
        r = client.get('/api/address-book/export?format=csv')
        assert r.status_code == 200
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert rows[0][0] == 'wxid'
        assert len(rows) == 4

    def test_xlsx(self, client):
        openpyxl = pytest.importorskip('openpyxl')
        r = client.get('/api/address-book/export?format=xlsx')
        assert r.status_code == 200
        assert r.mimetype == ('application/vnd.openxmlformats-officedocument'
                              '.spreadsheetml.sheet')
        ws = openpyxl.load_workbook(io.BytesIO(r.data)).active
        assert ws.max_row == 4
        assert ws.cell(row=2, column=2).value  # 有数据

    def test_html(self, client):
        r = client.get('/api/address-book/export?format=html')
        assert r.status_code == 200
        assert r.mimetype == 'text/html'
        html = r.data.decode('utf-8')
        assert '&lt;script&gt;' in html          # 转义生效
        assert '<script>alert(1)</script>' not in html

    def test_unknown_format_is_400_json(self, client):
        r = client.get('/api/address-book/export?format=pdf')
        assert r.status_code == 400
        assert 'pdf' in r.get_json()['error']

    def test_content_disposition_has_filename_with_extension(self, client):
        for fmt, ext in (('csv', '.csv'), ('xlsx', '.xlsx'), ('html', '.html')):
            r = client.get('/api/address-book/export?format=' + fmt)
            cd = r.headers['Content-Disposition']
            assert 'attachment' in cd
            assert ext in cd


class TestExportFilters:
    def test_kind_groups(self, client):
        r = client.get('/api/address-book/export?format=csv&kind=groups')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert len(rows) == 2                     # 表头 + 1 个群
        assert rows[1][12] == 'Y'

    def test_query_filter(self, client):
        r = client.get('/api/address-book/export?format=csv&q=lisi')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert len(rows) == 2
        assert rows[1][0] == 'wxid_alpha'

    def test_has_chat_filter(self, client):
        r = client.get('/api/address-book/export?format=csv&has_chat=1')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert len(rows) == 3                     # 表头 + 2 个有聊天的

    def test_sort_applied(self, client):
        r = client.get('/api/address-book/export?format=csv&sort=msg_count')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        counts = [int(row[10]) for row in rows[1:]]
        assert counts == sorted(counts, reverse=True)

    def test_label_filter(self, client):
        r = client.get('/api/address-book/export?format=csv&label=only_work')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert len(rows) == 3          # 表头 + 2 个 only_work

    def test_labels_column_exported(self, client):
        r = client.get('/api/address-book/export?format=csv')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        assert rows[0][-1] == 'labels'
        assert rows[0][:13] == ['wxid', 'display_name', 'remark', 'nick_name',
                                'alias', 'phone', 'sex', 'region', 'signature',
                                'description', 'msg_count', 'last_msg_time',
                                'is_group']
        beta = [row for row in rows[1:] if row[0] == 'wxid_beta'][0]
        assert beta[-1] == 'non_work,only_work'

    def test_phone_and_sex_and_region_now_populated(self, client):
        """旧版本这三列永远是空的（列不存在），现在应有真实值。"""
        r = client.get('/api/address-book/export?format=csv&label=only_work')
        rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        alpha = [row for row in rows[1:] if row[0] == 'wxid_alpha'][0]
        assert alpha[5] == '13800000001'          # phone
        assert alpha[6] == '女'                    # sex
        assert alpha[7] == "CN Shaanxi Xi'an"     # region
        assert alpha[8] == '签名A'                 # signature

    def test_export_does_not_mutate_cache(self, client):
        client.get('/api/address-book/export?format=csv&sort=msg_count')
        after = client.get('/api/address-book').get_json()['contacts']
        names = [c['display_name'].lower() for c in after]
        assert names == sorted(names)


class TestExportEdgeCases:
    def test_empty_address_book_still_exports(self, tmp_path, monkeypatch):
        d = tmp_path / 'empty'
        d.mkdir()
        app = create_app(str(d), wxid='wxid_example12345')
        app.config['TESTING'] = True
        monkeypatch.setattr(api_module, 'get_all_contacts', lambda _dir: [])
        c = app.test_client()
        for fmt in ('csv', 'xlsx', 'html'):
            r = c.get('/api/address-book/export?format=' + fmt)
            assert r.status_code == 200, fmt
            assert len(r.data) > 0

    def test_format_is_case_insensitive(self, client):
        assert client.get('/api/address-book/export?format=CSV').status_code == 200


class TestContactsPageHasExportControl:
    """UI 层的存在性检查：按钮/下拉被删掉时这里会红。"""

    def test_page_renders_export_control(self, client):
        r = client.get('/contacts')
        assert r.status_code == 200
        html = r.data.decode('utf-8')
        assert 'contacts-export-btn' in html
        assert 'contacts-export-format' in html
        for fmt in ('xlsx', 'csv', 'html'):
            assert 'value="%s"' % fmt in html

    def test_page_renders_label_filter(self, client):
        html = client.get('/contacts').data.decode('utf-8')
        assert 'contacts-label-filter' in html

    def test_export_script_is_loaded(self, client):
        html = client.get('/contacts').data.decode('utf-8')
        assert 'js/contacts-app.js' in html


class TestGroupsCarryLabels:
    def test_groups_endpoint_includes_labels(self, tmp_path, monkeypatch):
        """群聊列表也要带标签，否则群聊页的标签筛选会全空。"""
        d = tmp_path / 'grp'
        (d / 'data').mkdir(parents=True)
        import sqlite3
        con = sqlite3.connect(str(d / 'data' / 'chats.db'))
        con.execute('CREATE TABLE contacts (wxid TEXT, display_name TEXT, is_group INTEGER,'
                    ' remark TEXT, nick_name TEXT, alias TEXT)')
        con.execute('INSERT INTO contacts VALUES (?,?,?,?,?,?)',
                    ('88888888@chatroom', '工作群', 1, '', '', ''))
        con.close()
        app = create_app(str(d), wxid='wxid_example12345')
        app.config['TESTING'] = True
        monkeypatch.setattr(api_module, 'get_all_groups',
                            lambda _dir: [_contact(wxid='88888888@chatroom',
                                                   display_name='工作群',
                                                   is_group=True,
                                                   labels=['家人'])])
        c = app.test_client()
        got = c.get('/api/address-book/groups').get_json()['groups'][0]
        assert got['labels'] == ['家人']
