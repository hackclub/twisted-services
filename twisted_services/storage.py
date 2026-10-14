import hashlib
import os
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import DateTime, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
	pass


class SlackInstallation(Base):
	__tablename__ = "slack_installations"
	__table_args__ = (UniqueConstraint("team_id", "slack_user_id"),)

	id: Mapped[int] = mapped_column(primary_key=True)
	team_id: Mapped[str] = mapped_column(String(32), index=True)
	slack_user_id: Mapped[str] = mapped_column(String(32), index=True)
	token_ciphertext: Mapped[str] = mapped_column(String(512))
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
	updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class OAuthSession(Base):
	__tablename__ = "oauth_sessions"

	id: Mapped[int] = mapped_column(primary_key=True)
	session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
	team_id: Mapped[str] = mapped_column(String(32))
	slack_user_id: Mapped[str] = mapped_column(String(32), index=True)
	expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def database_url() -> str:
	url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./twisted_services.db")
	if url.startswith("postgresql://"):
		return url.replace("postgresql://", "postgresql+asyncpg://", 1)
	if url.startswith("postgres://"):
		return url.replace("postgres://", "postgresql+asyncpg://", 1)
	return url


def build_database() -> tuple[object, async_sessionmaker[AsyncSession]]:
	engine = create_async_engine(database_url(), pool_pre_ping=True)
	return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: object) -> None:
	async with engine.begin() as connection:
		await connection.run_sync(Base.metadata.create_all)


def encryption_key() -> bytes | None:
	value = os.getenv("OAUTH_TOKEN_ENCRYPTION_KEY", "")
	return value.encode("ascii") if value else None


def encrypt_token(token: str) -> str:
	key = encryption_key()
	if not key:
		raise RuntimeError("OAUTH_TOKEN_ENCRYPTION_KEY is not configured")
	return Fernet(key).encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
	key = encryption_key()
	if not key:
		raise RuntimeError("OAUTH_TOKEN_ENCRYPTION_KEY is not configured")
	return Fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")


def new_session_token() -> str:
	return secrets.token_urlsafe(32)


def session_digest(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def find_installation(session: AsyncSession, team_id: str, slack_user_id: str) -> SlackInstallation | None:
	result = await session.scalar(select(SlackInstallation).where(
		SlackInstallation.team_id == team_id,
		SlackInstallation.slack_user_id == slack_user_id,
	))
	return result


async def find_session(session: AsyncSession, token: str) -> OAuthSession | None:
	result = await session.scalar(select(OAuthSession).where(
		OAuthSession.session_hash == session_digest(token),
		OAuthSession.expires_at > datetime.now(timezone.utc),
	))
	return result