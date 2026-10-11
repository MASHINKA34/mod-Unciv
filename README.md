# mod-Unciv

Мод для [Unciv](https://github.com/yairm210/Unciv) — open-source ремейка Civilization V.

> Описание мода появится, когда будет готов первый контент.

## Установка на Android

1. Установите Unciv (Google Play, F-Droid или `Unciv-signed.apk` из
   [релизов](https://github.com/yairm210/Unciv/releases)).
2. Главное меню → **«Моды»** → **«Скачать мод по URL»**.
3. Вставьте ссылку `https://github.com/MASHINKA34/mod-Unciv` → **«Скачать»**.
4. Новая игра: **«Начать новую игру»** → **«Моды-расширения»** → отметьте мод.

Обновление: «Моды» → **«Обновить [мод]»** (или удалить и скачать заново).

## Installation (English)

Main menu → **Mods** → **Download mod from URL** → `https://github.com/MASHINKA34/mod-Unciv`.
Then enable it under **Extension mods** when starting a new game.

## Разработка

- Шпаргалка по модам Unciv: [docs/UNCIV_MODDING.md](docs/UNCIV_MODDING.md)
- Проверка и упаковка картинок (нужны Python 3 и Java 21):

  ```
  python tools/check_mod.py
  ```

  Android не умеет упаковывать картинки, поэтому `game.atlas`, `game*.png` и
  `Atlases.json` собираются этой командой и коммитятся в репозиторий.

## Авторы

См. [Credits.md](Credits.md).
