"""Study management -- syncs study configuration to the database."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from typing import TYPE_CHECKING

from researchloop.core.config import StudyConfig

if TYPE_CHECKING:
    from researchloop.core.config import ClusterConfig, Config
    from researchloop.db.database import Database

from researchloop.db import queries

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _serialize(cfg: StudyConfig) -> str:
    """Deterministically serialize a StudyConfig for storage/comparison."""
    return json.dumps(asdict(cfg), sort_keys=True)


class StudyManager:
    """High-level operations on research studies.

    The database is the runtime source of truth for studies. The TOML
    config seeds the database on startup and provides the revert target
    for UI edits.
    """

    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # ------------------------------------------------------------------
    # Sync from config
    # ------------------------------------------------------------------

    async def sync_from_config(self) -> None:
        """Reconcile DB studies with the TOML config.

        The DB is authoritative for runtime; this method updates the
        ``yaml_config_json`` snapshot (used by "Revert to YAML") and
        adopts new TOML values for studies that have not been edited
        through the UI.
        """
        yaml_names: set[str] = set()
        for study_cfg in self.config.studies:
            yaml_names.add(study_cfg.name)
            yaml_json = _serialize(study_cfg)
            existing = await queries.get_study(self.db, study_cfg.name)

            if existing is None:
                logger.info("Creating study %r in database", study_cfg.name)
                await queries.create_study(
                    self.db,
                    name=study_cfg.name,
                    cluster=study_cfg.cluster,
                    description=study_cfg.description or None,
                    claude_md_path=study_cfg.claude_md_path or None,
                    sprints_dir=study_cfg.sprints_dir,
                    config_json=yaml_json,
                    source="yaml",
                    yaml_config_json=yaml_json,
                )
                continue

            prev_yaml = existing.get("yaml_config_json")
            current = existing.get("config_json")
            ui_edited = prev_yaml is not None and current != prev_yaml

            if ui_edited:
                logger.info(
                    "Preserving UI edits for study %r; refreshing YAML snapshot",
                    study_cfg.name,
                )
                await queries.update_study(
                    self.db,
                    study_cfg.name,
                    yaml_config_json=yaml_json,
                )
            else:
                logger.info("Updating study %r from YAML", study_cfg.name)
                await queries.update_study(
                    self.db,
                    study_cfg.name,
                    cluster=study_cfg.cluster,
                    description=study_cfg.description or None,
                    claude_md_path=study_cfg.claude_md_path or None,
                    sprints_dir=study_cfg.sprints_dir,
                    config_json=yaml_json,
                    yaml_config_json=yaml_json,
                    source="yaml",
                )

        # DB rows no longer in YAML: drop the snapshot and flip to 'ui'
        # so the user can edit/delete them from the dashboard.
        all_rows = await queries.list_studies(self.db)
        for row in all_rows:
            if row["name"] in yaml_names:
                continue
            if row.get("yaml_config_json") is not None or row.get("source") == "yaml":
                logger.info(
                    "Study %r no longer in YAML; converting to UI-only",
                    row["name"],
                )
                await queries.update_study(
                    self.db,
                    row["name"],
                    yaml_config_json=None,
                    source="ui",
                )

        await self._rebuild_config_studies()
        logger.info(
            "Study sync complete: %d study/studies processed",
            len(self.config.studies),
        )

    async def _rebuild_config_studies(self) -> None:
        """Replace ``self.config.studies`` in place from the DB.

        We mutate the existing list (``clear`` + ``extend``) so that any
        external code holding a reference to it sees the updates.
        """
        rows = await queries.list_studies(self.db)
        rebuilt: list[StudyConfig] = []
        for r in rows:
            cj = r.get("config_json")
            if not cj:
                continue
            try:
                data = json.loads(cj)
            except json.JSONDecodeError:
                logger.warning("Skipping study %r: invalid config_json", r["name"])
                continue
            try:
                rebuilt.append(StudyConfig(**data))
            except TypeError:
                logger.warning(
                    "Skipping study %r: config_json missing required fields",
                    r["name"],
                )
        self.config.studies.clear()
        self.config.studies.extend(rebuilt)

    # ------------------------------------------------------------------
    # UI mutations
    # ------------------------------------------------------------------

    async def create_study_from_ui(self, cfg: StudyConfig) -> None:
        """Create a brand-new UI-defined study."""
        self._validate(cfg)
        existing = await queries.get_study(self.db, cfg.name)
        if existing is not None:
            raise ValueError(f"Study {cfg.name!r} already exists")
        await queries.create_study(
            self.db,
            name=cfg.name,
            cluster=cfg.cluster,
            description=cfg.description or None,
            claude_md_path=cfg.claude_md_path or None,
            sprints_dir=cfg.sprints_dir,
            config_json=_serialize(cfg),
            source="ui",
            yaml_config_json=None,
        )
        await self._rebuild_config_studies()

    async def update_study_from_ui(self, name: str, cfg: StudyConfig) -> None:
        """Update an existing study with values from the UI."""
        existing = await queries.get_study(self.db, name)
        if existing is None:
            raise ValueError(f"Study not found: {name}")
        if cfg.name != name:
            raise ValueError("Renaming studies is not supported")
        self._validate(cfg)
        await queries.update_study(
            self.db,
            name,
            cluster=cfg.cluster,
            description=cfg.description or None,
            claude_md_path=cfg.claude_md_path or None,
            sprints_dir=cfg.sprints_dir,
            config_json=_serialize(cfg),
        )
        await self._rebuild_config_studies()

    async def revert_study_to_yaml(self, name: str) -> None:
        """Restore a study's ``config_json`` from the YAML snapshot."""
        row = await queries.get_study(self.db, name)
        if row is None:
            raise ValueError(f"Study not found: {name}")
        yaml_json = row.get("yaml_config_json")
        if not yaml_json:
            raise ValueError("No YAML version available to revert to")
        data = json.loads(yaml_json)
        await queries.update_study(
            self.db,
            name,
            cluster=data.get("cluster", row["cluster"]),
            description=data.get("description") or None,
            claude_md_path=data.get("claude_md_path") or None,
            sprints_dir=data.get("sprints_dir") or row["sprints_dir"],
            config_json=yaml_json,
        )
        await self._rebuild_config_studies()

    async def delete_ui_study(self, name: str) -> None:
        """Delete a study that was created purely through the UI."""
        row = await queries.get_study(self.db, name)
        if row is None:
            raise ValueError(f"Study not found: {name}")
        if row.get("source") != "ui" or row.get("yaml_config_json"):
            raise ValueError(
                "Only UI-only studies can be deleted; "
                "revert a YAML study to restore the TOML version instead"
            )
        n = await queries.count_sprints_for_study(self.db, name)
        if n > 0:
            raise ValueError(f"Cannot delete: {n} sprint(s) reference this study")
        await queries.delete_study(self.db, name)
        await self._rebuild_config_studies()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, cfg: StudyConfig) -> None:
        if not cfg.name:
            raise ValueError("Study name is required")
        if not _NAME_RE.fullmatch(cfg.name):
            raise ValueError(
                "Name must be lowercase letters, digits, and hyphens "
                "(matching ^[a-z0-9][a-z0-9-]*$)"
            )
        if not cfg.cluster:
            raise ValueError("Cluster is required")
        cluster_names = {c.name for c in self.config.clusters}
        if cfg.cluster not in cluster_names:
            raise ValueError(f"Cluster {cfg.cluster!r} is not defined")
        if cfg.max_sprint_duration_hours < 1:
            raise ValueError("max_sprint_duration_hours must be >= 1")
        if cfg.red_team_max_rounds < 0:
            raise ValueError("red_team_max_rounds must be >= 0")
        for k, v in cfg.job_options.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("job_options keys and values must be strings")

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def get(self, name: str) -> dict | None:
        """Return a single study by name, or ``None``."""
        return await queries.get_study(self.db, name)

    async def list_all(self) -> list[dict]:
        """Return all studies."""
        return await queries.list_studies(self.db)

    async def get_cluster_config(self, study_name: str) -> ClusterConfig:
        """Return the :class:`ClusterConfig` for the cluster associated
        with *study_name*.

        Raises :class:`ValueError` if the study or cluster is not found.
        """
        study = await queries.get_study(self.db, study_name)
        if study is None:
            raise ValueError(f"Study not found: {study_name}")

        cluster_name = study["cluster"]
        for cluster in self.config.clusters:
            if cluster.name == cluster_name:
                return cluster

        raise ValueError(
            f"Cluster {cluster_name!r} (referenced by study "
            f"{study_name!r}) not found in config"
        )
