"""ORM mappings for schema-critical tenant records."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OnboardingCase(Base):
    __tablename__ = "onboarding_cases"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    parent_case_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    workflow_root_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    workflow_name: Mapped[str | None] = mapped_column(Text)
    discovery_context: Mapped[dict] = mapped_column(JSONB)


class Defect(Base):
    __tablename__ = "defects"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    patch_build_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    workflow_case_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class BlueprintCandidate(Base):
    __tablename__ = "blueprint_candidates"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    proposal: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    candidate_order: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimDecision(Base):
    __tablename__ = "claim_decisions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    claim_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(Text)
    decision_order: Mapped[int] = mapped_column(BigInteger)


class OrganizationProfileRevision(Base):
    __tablename__ = "organization_profile_revisions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    about_organization: Mapped[str] = mapped_column(Text)
    profile_order: Mapped[int] = mapped_column(BigInteger)


class ConversationMemorySummary(Base):
    __tablename__ = "conversation_memory_summaries"

    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    chat_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    chat_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    content: Mapped[str | None] = mapped_column(Text)
    working_content: Mapped[str] = mapped_column(Text)
    compacted_message_count: Mapped[int] = mapped_column(Integer)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(Text)
    baseline: Mapped[bool]


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    review_state: Mapped[str] = mapped_column(Text)
