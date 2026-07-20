"""Simple repository layer for common models.
This implements a repository/service architecture for accessing DB entities.
"""
from typing import Optional, List
from db.session import get_session
from db.models import User, Chat, Message, Agent, FileMeta, Setting, Knowledge, APIKey as APIKeyModel, Session as DBSess
from sqlalchemy.orm import Session
import secrets

class UsersRepo:
    def __init__(self):
        pass

    def get_by_username(self, username: str) -> Optional[User]:
        try:
            db: Session = get_session()
        except Exception:
            return None
        try:
            return db.query(User).filter(User.username == username).first()
        finally:
            db.close()

    def create_user(self, username: str, hashed_password: str, role: str = 'user') -> User:
        db: Session = get_session()
        try:
            u = User(username=username, hashed_password=hashed_password, role=role)
            db.add(u)
            db.commit()
            db.refresh(u)
            return u
        finally:
            db.close()

class ChatsRepo:
    def create_chat(self, title: str, owner_id: int):
        db = get_session()
        try:
            c = Chat(title=title, owner_id=owner_id)
            db.add(c); db.commit(); db.refresh(c)
            return c
        finally:
            db.close()

class MessagesRepo:
    def add_message(self, chat_id: int, role: str, content: str, model: str = '', provider: str = '', tokens: int = 0):
        db = get_session()
        try:
            m = Message(chat_id=chat_id, role=role, content=content, model=model, provider=provider, tokens=tokens)
            db.add(m); db.commit(); db.refresh(m)
            return m
        finally:
            db.close()

class SettingsRepo:
    def get(self, key: str) -> Optional[str]:
        try:
            db = get_session()
        except Exception:
            return None
        try:
            s = db.query(Setting).filter(Setting.key == key).first()
            return s.value if s else None
        finally:
            db.close()

    def set(self, key: str, value: str) -> None:
        db = get_session()
        try:
            s = db.query(Setting).filter(Setting.key == key).first()
            if s:
                s.value = value
            else:
                s = Setting(key=key, value=value)
                db.add(s)
            db.commit()
        finally:
            db.close()

class APIKeyRepo:
    def get_by_label(self, label: str) -> Optional[APIKeyModel]:
        try:
            db = get_session()
        except Exception:
            return None
        try:
            return db.query(APIKeyModel).filter(APIKeyModel.label == label).first()
        finally:
            db.close()

    def create(self, user_id: int, role: str = 'user', label: str = '') -> str:
        db = get_session()
        try:
            key = 'ys_' + secrets.token_hex(32)
            ak = APIKeyModel(key=key, user_id=user_id, role=role, label=label)
            db.add(ak)
            db.commit()
            return key
        finally:
            db.close()

