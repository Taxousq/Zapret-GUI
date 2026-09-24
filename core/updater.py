"""Обновление запрета: скачивание последнего релиза с GitHub и распаковка.

Используется GitHub API (``/releases/latest``). Из ассетов релиза берётся
``.zip``, скачивается во временную папку, распаковывается и копируется в папку
запрета с заменой существующих файлов.

Пользовательские списки (``*-user.*``) не перезаписываются: их правки терять
нельзя, а service.bat сам управляет ими отдельно.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from config import (
    GITHUB_ACCEPT,
    GITHUB_API_LATEST,
    UPDATE_PRESERVE_PATTERNS,
    UPDATE_TIMEOUT,
    ZAPRET_PATH,
)

log = logging.getLogger(__name__)

#: Прогресс скачивания: (сколько скачано байт, всего байт или 0).
ProgressCallback = Callable[[int, int], None]


class UpdaterError(RuntimeError):
    """Понятная пользователю ошибка обновления."""


@dataclass
class UpdateInfo:
    """Сведения о последнем релизе на GitHub."""

    version: str
    tag: str
    name: str
    published_at: str
    notes: str
    html_url: str
    asset_name: str
    asset_url: str
    asset_size: int

    @property
    def size_text(self) -> str:
        if self.asset_size <= 0:
            return "—"
        return f"{self.asset_size / 1024 / 1024:.1f} МБ"


@dataclass
class UpdateCheck:
    """Результат проверки обновления."""

    local_version: str
    latest: UpdateInfo | None = None
    has_update: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _version_key(version: str) -> tuple[int, ...]:
    """«1.10.3» -> (1, 10, 3) — для сравнения версий."""
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers) if numbers else (0,)


class Updater:
    """Проверяет и устанавливает обновления запрета."""

    def __init__(self, target_dir: Path | None = None) -> None:
        self.target_dir = Path(target_dir) if target_dir else Path(ZAPRET_PATH)

    # -- версии ------------------------------------------------------------
    def get_local_version(self) -> str:
        """Версия установленного запрета.

        Сначала ищем ``.service\\version.txt``, затем — ``LOCAL_VERSION``
        внутри service.bat (так версия хранится в свежих релизах).
        """
        version_file = self.target_dir / ".service" / "version.txt"
        try:
            if version_file.is_file():
                text = version_file.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s", version_file, exc)

        service_bat = self.target_dir / "service.bat"
        try:
            if service_bat.is_file():
                text = service_bat.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'LOCAL_VERSION\s*=\s*"?([\d][\d.]*)"?', text)
                if match:
                    return match.group(1)
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s", service_bat, exc)

        return "неизвестно"

    # -- сеть --------------------------------------------------------------
    def _get(self, url: str, **kwargs) -> requests.Response:
        headers = {
            "Accept": GITHUB_ACCEPT,
            "User-Agent": "ZapretGUI",
        }
        headers.update(kwargs.pop("headers", {}))
        return requests.get(url, headers=headers, timeout=UPDATE_TIMEOUT, **kwargs)

    def get_latest(self) -> UpdateInfo:
        """Возвращает сведения о последнем релизе.

        :raises UpdaterError: если сеть недоступна или в релизе нет .zip.
        """
        try:
            response = self._get(GITHUB_API_LATEST)
        except requests.exceptions.Timeout as exc:
            raise UpdaterError(
                "GitHub не ответил вовремя. Проверьте подключение к интернету."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise UpdaterError(f"Не удалось связаться с GitHub:\n{exc}") from exc

        if response.status_code == 403:
            raise UpdaterError(
                "GitHub отклонил запрос (403): превышен лимит обращений.\n"
                "Попробуйте позже."
            )
        if response.status_code == 404:
            raise UpdaterError("У репозитория запрета нет опубликованных релизов.")
        if response.status_code != 200:
            raise UpdaterError(
                f"GitHub вернул код {response.status_code}.\n{response.text[:300]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise UpdaterError("GitHub вернул ответ в неизвестном формате.") from exc

        assets = data.get("assets") or []
        asset = next(
            (a for a in assets if str(a.get("name", "")).lower().endswith(".zip")), None
        )
        if asset is None:
            names = ", ".join(str(a.get("name")) for a in assets) or "нет"
            raise UpdaterError(
                f"В релизе {data.get('tag_name')} нет .zip-архива.\nДоступные файлы: {names}"
            )

        return UpdateInfo(
            version=str(data.get("tag_name") or data.get("name") or ""),
            tag=str(data.get("tag_name") or ""),
            name=str(data.get("name") or ""),
            published_at=str(data.get("published_at") or ""),
            notes=str(data.get("body") or ""),
            html_url=str(data.get("html_url") or ""),
            asset_name=str(asset.get("name") or ""),
            asset_url=str(asset.get("browser_download_url") or ""),
            asset_size=int(asset.get("size") or 0),
        )

    def check(self) -> UpdateCheck:
        """Проверяет наличие новой версии. Не выбрасывает исключений."""
        local = self.get_local_version()
        try:
            latest = self.get_latest()
        except UpdaterError as exc:
            return UpdateCheck(
                local_version=local, message=str(exc), errors=[str(exc)]
            )

        has_update = _version_key(latest.version) > _version_key(local)
        if has_update:
            message = f"Доступна новая версия: {latest.version} (установлена {local})."
        else:
            message = f"Установлена последняя версия: {local}."
        return UpdateCheck(
            local_version=local, latest=latest, has_update=has_update, message=message
        )

    # -- скачивание --------------------------------------------------------
    def download(
        self,
        info: UpdateInfo,
        on_progress: ProgressCallback | None = None,
        workdir: Path | None = None,
    ) -> Path:
        """Скачивает .zip-архив релиза и возвращает путь к нему."""
        workdir = Path(workdir) if workdir else Path(
            tempfile.mkdtemp(prefix="zapret-update-")
        )
        workdir.mkdir(parents=True, exist_ok=True)
        destination = workdir / (info.asset_name or "zapret-update.zip")

        try:
            with self._get(info.asset_url, stream=True) as response:
                if response.status_code != 200:
                    raise UpdaterError(
                        f"Не удалось скачать архив (код {response.status_code})."
                    )
                total = int(response.headers.get("Content-Length") or info.asset_size or 0)
                downloaded = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            on_progress(downloaded, total)
        except requests.exceptions.RequestException as exc:
            raise UpdaterError(f"Ошибка при скачивании архива:\n{exc}") from exc
        except OSError as exc:
            raise UpdaterError(f"Не удалось сохранить архив:\n{exc}") from exc

        if not zipfile.is_zipfile(destination):
            raise UpdaterError("Скачанный файл не является zip-архивом.")
        return destination

    def extract(self, archive: Path, into: Path) -> Path:
        """Распаковывает архив и возвращает корень с файлами релиза."""
        into.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                # Защита от путей вида ../../ — распаковываем строго внутрь into.
                target = (into / member.filename).resolve()
                if not str(target).startswith(str(into.resolve())):
                    raise UpdaterError(
                        f"Архив содержит небезопасный путь: {member.filename}"
                    )
            zf.extractall(into)

        # Если внутри архива одна папка — работаем с её содержимым.
        entries = [p for p in into.iterdir() if p.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return into

    def install_from_directory(
        self, source: Path, on_status: Callable[[str], None] | None = None
    ) -> tuple[bool, str]:
        """Копирует файлы из source в папку запрета с заменой существующих."""
        if not self.target_dir.is_dir():
            return False, f"Папка запрета не найдена:\n{self.target_dir}"

        copied = 0
        skipped_user: list[str] = []
        failed: list[str] = []

        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            name_lower = item.name.lower()
            if any(pattern in name_lower for pattern in UPDATE_PRESERVE_PATTERNS):
                skipped_user.append(str(relative))
                continue
            destination = self.target_dir / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                copied += 1
                if on_status is not None and copied % 20 == 0:
                    on_status(f"Скопировано файлов: {copied}")
            except OSError as exc:
                log.error("Не удалось скопировать %s: %s", relative, exc)
                failed.append(f"{relative}: {exc}")

        if failed and copied == 0:
            return False, (
                "Не удалось обновить файлы.\n"
                "Скорее всего, служба запущена и winws.exe заблокирован — "
                "остановите службу и повторите.\n\n" + "\n".join(failed[:5])
            )

        message = f"Обновление применено. Скопировано файлов: {copied}."
        if skipped_user:
            message += (
                f"\nСохранены пользовательские списки ({len(skipped_user)} шт.), "
                "они не перезаписывались."
            )
        if failed:
            message += f"\nНе удалось заменить файлов: {len(failed)}."
        return True, message

    def download_and_install(
        self,
        info: UpdateInfo,
        on_progress: ProgressCallback | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Скачивает и устанавливает обновление. Исключения не выбрасываются."""
        workdir: Path | None = None
        try:
            if on_status is not None:
                on_status("Скачивание архива...")
            workdir = Path(tempfile.mkdtemp(prefix="zapret-update-"))
            archive = self.download(info, on_progress=on_progress, workdir=workdir)

            if on_status is not None:
                on_status("Распаковка архива...")
            source = self.extract(archive, workdir / "extracted")

            if on_status is not None:
                on_status("Копирование файлов...")
            return self.install_from_directory(source, on_status=on_status)
        except UpdaterError as exc:
            log.error("Обновление не удалось: %s", exc)
            return False, str(exc)
        except (OSError, zipfile.BadZipFile) as exc:
            log.exception("Обновление не удалось")
            return False, f"Не удалось установить обновление:\n{exc}"
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
