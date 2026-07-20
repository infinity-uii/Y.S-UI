from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from db.session import Base

now = lambda: datetime.utcnow()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String(128), unique=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(64), default='user')
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now)

class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)
    title = Column(String(255))
    owner_id = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now)
    messages = relationship('Message', back_populates='chat')

class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, ForeignKey('chats.id'))
    role = Column(String(32))
    content = Column(Text)
    model = Column(String(128))
    provider = Column(String(128))
    tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)
    chat = relationship('Chat', back_populates='messages')

class Agent(Base):
    __tablename__ = 'agents'
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    label = Column(String(255))
    role = Column(String(128))
    status = Column(String(64), default='ready')
    created_at = Column(DateTime, default=now)

class FileMeta(Base):
    __tablename__ = 'files'
    id = Column(Integer, primary_key=True)
    path = Column(String(1024))
    name = Column(String(255))
    size = Column(Integer)
    uploaded_by = Column(Integer, ForeignKey('users.id'))
    uploaded_at = Column(DateTime, default=now)

class Setting(Base):
    __tablename__ = 'settings'
    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)
    updated_at = Column(DateTime, default=now)

class Knowledge(Base):
    __tablename__ = 'knowledge'
    id = Column(Integer, primary_key=True)
    source = Column(String(255))
    text = Column(Text)
    metadata = Column(Text)
    created_at = Column(DateTime, default=now)

class APIKey(Base):
    __tablename__ = 'api_keys'
    id = Column(Integer, primary_key=True)
    key = Column(String(128), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'))
    role = Column(String(64), default='user')
    label = Column(String(255))
    created_at = Column(DateTime, default=now)

class Session(Base):
    __tablename__ = 'sessions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    session_token = Column(String(256), unique=True, nullable=False)
    created_at = Column(DateTime, default=now)
    expires_at = Column(DateTime)
