"""Small, offline account recovery commands for the site owner."""
import argparse
import os
from pathlib import Path
import secrets

from auth_store import AuthStore, password_hash, validate


BASE = Path(__file__).resolve().parent


def state_root():
    raw = os.environ.get('STATE_DIR', '').strip()
    path = Path(raw).expanduser() if raw else BASE / 'data'
    if not path.is_absolute():
        path = BASE / path
    return path.resolve()


def reset_admin():
    store = AuthStore(state_root() / 'accounts.sqlite3')
    password = os.environ.get('ADMIN_RESET_PASSWORD') or secrets.token_urlsafe(20)
    validate('admin', password)
    with store.db() as db:
        row = db.execute("SELECT id,username FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if not row:
            raise SystemExit('当前数据库中没有管理员账号，请先启动一次网站完成初始化。')
        db.execute('UPDATE users SET password_hash=?,enabled=1 WHERE id=?', (password_hash(password), row['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
        store.event(db, row['username'], 'password_reset_cli', True, 'local-console', 'manage.py', uid=row['id'])
    print('管理员密码已重置，全部旧登录已失效。')
    print('管理员用户名：' + row['username'])
    print('新密码：' + password)
    print('请立即登录并在“账号设置”中更换密码。')


def main():
    parser = argparse.ArgumentParser(description='航迹网站账号维护')
    parser.add_argument('command', choices=['reset-admin'])
    args = parser.parse_args()
    if args.command == 'reset-admin':
        reset_admin()


if __name__ == '__main__':
    main()
