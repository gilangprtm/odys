#!/usr/bin/env python3
"""setup_ecc_skills.py — Clone ECC repo and install skills/rules as Odys built-ins.

Usage:
    python scripts/setup_ecc_skills.py [--force] [--no-symlink]

Options:
    --force       Overwrite existing skills/rules
    --no-symlink  Copy instead of symlink (for Windows without admin)
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ECC_REPO = "https://github.com/affaan-m/ECC.git"
ECC_BRANCH = "main"
ECC_DEPTH = 1

# Target directories under user's Odys config
ODYS_HOME = Path.home() / ".odys"
SKILLS_DEST = ODYS_HOME / "skills"
RULES_DEST = ODYS_HOME / "rules"

# Source directories inside ECC repo
ECC_SKILLS_SRC = "skills"
ECC_INSTINCTS_SRC = ".agents/instincts"  # might not exist; fallback to docs/rules


def run_cmd(cmd: list[str], cwd: Path = None) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def clone_ecc(temp_dir: Path) -> Path:
    """Shallow clone ECC repo to temp directory. Returns path to repo."""
    repo_path = temp_dir / "ECC"
    if repo_path.exists():
        logger.info("ECC repo already exists, pulling latest...")
        run_cmd(["git", "pull", "origin", ECC_BRANCH], cwd=repo_path)
    else:
        logger.info("Cloning ECC repo (shallow)...")
        run_cmd(["git", "clone", "--depth", str(ECC_DEPTH), "--branch", ECC_BRANCH, ECC_REPO, str(repo_path)])
    return repo_path


def install_skills(ecc_repo: Path, force: bool = False, symlink: bool = True):
    """Install skills from ECC to Odys config."""
    src = ecc_repo / ECC_SKILLS_SRC
    if not src.exists():
        logger.error("ECC skills source not found at %s", src)
        return

    SKILLS_DEST.mkdir(parents=True, exist_ok=True)

    skill_dirs = [d for d in src.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    logger.info("Found %d ECC skills to install", len(skill_dirs))

    installed = 0
    skipped = 0
    for skill_dir in skill_dirs:
        name = skill_dir.name
        dest = SKILLS_DEST / name

        if dest.exists():
            if force:
                logger.info("Overwriting existing skill: %s", name)
                if dest.is_symlink() or os.path.islink(dest):
                    dest.unlink()
                else:
                    shutil.rmtree(dest)
            else:
                logger.debug("Skill already exists, skipping: %s", name)
                skipped += 1
                continue

        try:
            if symlink and hasattr(os, "symlink"):
                try:
                    rel_src = os.path.relpath(skill_dir, dest.parent)
                    dest.symlink_to(rel_src, target_is_directory=True)
                    logger.info("Symlinked skill: %s", name)
                except OSError:
                    # Fallback to copy if symlink privileges are missing (common on Windows)
                    shutil.copytree(skill_dir, dest)
                    logger.info("Copied skill (fallback): %s", name)
            else:
                shutil.copytree(skill_dir, dest)
                logger.info("Copied skill: %s", name)
            installed += 1
        except (OSError, shutil.Error) as e:
            logger.warning("Failed to install skill %s: %s", name, e)

    logger.info("Skills: %d installed, %d skipped", installed, skipped)


def install_rules(ecc_repo: Path, force: bool = False, symlink: bool = True):
    """Install instincts/rules from ECC to Odys config."""
    # ECC puts instincts in .agents/instincts (or sometimes in skills/*/instincts.md)
    # For now, copy .agents/instincts if it exists, else fall back to any docs/rules
    src_candidates = [
        ecc_repo / ECC_INSTINCTS_SRC,
        ecc_repo / "docs" / "rules",
        ecc_repo / ".claude" / "rules",
    ]

    src = None
    for c in src_candidates:
        if c.exists() and any(c.iterdir()):
            src = c
            break

    if src is None:
        logger.warning("No ECC instincts/rules source found; skipping")
        return

    RULES_DEST.mkdir(parents=True, exist_ok=True)

    rule_files = list(src.glob("*.md"))
    logger.info("Found %d rule files in %s", len(rule_files), src.relative_to(ecc_repo))

    installed = 0
    skipped = 0
    for rule_file in rule_files:
        name = rule_file.stem
        dest = RULES_DEST / f"{name}.md"

        if dest.exists():
            if force:
                dest.unlink()
            else:
                logger.debug("Rule already exists, skipping: %s", name)
                skipped += 1
                continue

        try:
            if symlink and hasattr(os, "symlink"):
                try:
                    rel_src = os.path.relpath(rule_file, dest.parent)
                    dest.symlink_to(rel_src)
                    logger.info("Symlinked rule: %s", name)
                except OSError:
                    shutil.copy2(rule_file, dest)
                    logger.info("Installed rule (fallback): %s", name)
            else:
                shutil.copy2(rule_file, dest)
                logger.info("Installed rule: %s", name)
            installed += 1
        except (OSError, shutil.Error) as e:
            logger.warning("Failed to install rule %s: %s", name, e)

    logger.info("Rules: %d installed, %d skipped", installed, skipped)


def main():
    parser = argparse.ArgumentParser(description="Install ECC skills/rules as Odys built-ins")
    parser.add_argument("--force", action="store_true", help="Overwrite existing")
    parser.add_argument("--no-symlink", action="store_true", help="Copy instead of symlink")
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="ecc_setup_") as tmp:
        temp_dir = Path(tmp)
        try:
            ecc_repo = clone_ecc(temp_dir)
            install_skills(ecc_repo, force=args.force, symlink=not args.no_symlink)
            install_rules(ecc_repo, force=args.force, symlink=not args.no_symlink)
            logger.info("Done! Skills at: %s", SKILLS_DEST)
            logger.info("Rules at: %s", RULES_DEST)
        except Exception as e:
            logger.error("Setup failed: %s", e)
            sys.exit(1)


if __name__ == "__main__":
    main()