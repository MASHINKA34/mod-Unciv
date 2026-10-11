# Шпаргалка по модам Unciv

Собрано из официальной документации и исходников Unciv 4.22.x (октябрь 2026).
Если что-то не сходится с игрой — первоисточник важнее, см. ссылки внизу.

## 1. Типы модов

| Тип | Что делает | Как включается |
|---|---|---|
| **Расширение** (extension) | Добавляет/меняет нации, юнитов, здания, ресурсы поверх базовых правил | «Начать новую игру» → «Моды-расширения» → галочка |
| **Базовый набор правил** (base ruleset) | Полностью заменяет правила игры. `"isBaseRuleset": true` в `jsons/ModOptions.json` | «Начать новую игру» → выбор базового набора правил |
| **Аудиовизуальный** | Только картинки/звуки/тайлсеты, без `jsons` | «Моды» → мод → «Перманентный аудиовизуальный мод» |

Мод **не может придумать новую механику** — только комбинировать существующие
*uniques* (строки-способности из фиксированного списка, см. ссылку ниже).
Начинать лучше с мода-расширения.

## 2. Структура (корень репозитория = корень мода)

```
jsons/                       правила игры (JSON)
  ModOptions.json            настройки мода (isBaseRuleset, удаление объектов, uniques мода)
  Nations.json Units.json Buildings.json Techs.json Policies.json UnitPromotions.json
  UnitTypes.json TileImprovements.json TileResources.json Terrains.json Beliefs.json
  Religions.json Specialists.json Eras.json Speeds.json Difficulties.json Events.json
  GlobalUniques.json VictoryTypes.json Quests.json Ruins.json CityStateTypes.json ...
  translations/Russian.properties   переводы
Images/                      исходные PNG (упаковываются в атлас)
game.atlas, game.png         упакованный атлас (+ game2.png ... если не влезло в 2048x2048)
Atlases.json                 список атласов (создаёт упаковщик, UTF-8 без BOM)
sounds/ music/ voices/       звуки (mp3/ogg/wav), музыка (mp3/ogg), голоса лидеров
maps/ scenarios/             карты из редактора, сценарии
preview.png | preview.jpg    превью в менеджере модов
README.md, Credits.md        описание и авторы картинок/звуков
```

В JSON-файле мода лежат **только новые/изменённые объекты** — копировать весь
файл базовой игры не нужно. Объект с тем же `name`, что в базе, заменяет базовый.

## 3. Ключевые приёмы в JSON

- Уникальный юнит/здание нации: `"uniqueTo": "<Нация>"` + `"replaces": "<Базовый объект>"`.
- Способности — массив `"uniques"`; условия в угловых скобках:
  `"[+15]% Strength <for [All] units> <when defending>"`.
- Скрытый unique: суффикс `<hidden from users>`; комментарий для игрока: `"Comment [текст]"`.
- Цвета нации: `"outerColor": [R,G,B]`, `"innerColor": [R,G,B]`.
- Все `name` пишутся **по-английски** — это ключи. Русский текст — только через переводы.
- Файлы — **строгий JSON** (без комментариев и висячих запятых), UTF-8 **без BOM**, отступ табами.
- Лучший учебник — базовые файлы игры и готовые моды (ссылки внизу).

## 4. Переводы

`jsons/translations/Russian.properties`, UTF-8 без BOM, строки вида:

```
Undead Warrior = Мёртвый воин
[+15]% Strength <when defending> = ...   # плейсхолдеры в [ ] не переводятся
```

Переводятся имена объектов, реплики лидера, названия городов, тексты uniques
(если они не из стандартного набора — стандартные игра переводит сама).

## 5. Картинки

Путь: `Images/<Папка>/<name из JSON>.png`. Имя файла = **точный `name`** из JSON
(регистр важен, пробелы допустимы), **только ASCII**.

| Что | Папка | Размер в базовой игре | Стиль |
|---|---|---|---|
| Юнит (иконка) | `UnitIcons/` | 100×100 | белый силуэт на прозрачном фоне |
| Здание / чудо | `BuildingIcons/` | 100×100 | белый силуэт на прозрачном |
| Нация (герб) | `NationIcons/` | 100×100 | белый силуэт на прозрачном |
| Технология | `TechIcons/` | 100×100 | белый силуэт на прозрачном |
| Ресурс | `ResourceIcons/` | 100×100 | белый силуэт на прозрачном |
| Улучшение клетки | `ImprovementIcons/` | 100×100 | белый силуэт на прозрачном |
| Политика | `PolicyIcons/` | 100×100 | белый силуэт на прозрачном |
| Повышение юнита | `UnitPromotionIcons/` | 50×50 | белый силуэт на прозрачном |
| Портрет лидера | `LeaderIcons/` | квадрат 100–256 | **цветной**, прозрачная рамка |
| Портреты юнитов/зданий | `UnitPortraits/`, `BuildingPortraits/` ... | квадрат 100–256 | **цветной** |
| Заставка чуда | `WonderImages/` | 2:1, напр. 200×100 | цветная |
| Спрайт юнита на карте (тайлсет FantasyHex) | `TileSets/FantasyHex/Units/` | 32×28 | **пиксель-арт**, цветной |

- Иконки игра **сама красит** в цвета цивилизации и кладёт на круг, поэтому они
  белые (с альфой), без фона и без своего круга.
- Документация для новых наций советует 200×200 для юнитов/зданий — больше
  можно, игра масштабирует; базовая игра использует 100×100.
- Пиксель-арт на маленьких иконках размывается фильтром `MipMapLinearLinear`:
  рисовать в низком разрешении и экспортировать с целым nearest-neighbor
  увеличением до размера из таблицы.

### Атлас — главное ограничение Android

Игра видит только **упакованный атлас** (`game.atlas` + `game.png`), а не сами PNG.
**Android упаковывать не умеет**, поэтому упаковка делается на ПК и результат
коммитится в репозиторий:

```
python tools/check_mod.py
```

Скрипт качает официальный `Unciv.jar` и запускает `java -jar Unciv.jar mod-ci`:
упаковка + автообновление устаревших uniques + проверка правил. Нужна Java 21.

## 6. Как игра скачивает мод с GitHub

1. Берёт zip ветки по умолчанию (`main`). GitHub собирает zip через `git archive`,
   поэтому всё с `export-ignore` в `.gitattributes` **не попадает к игрокам**.
2. Проверяет корень архива: «известных» записей (`Images`, `Images.*`, `jsons`,
   `maps`, `music`, `scenarios`, `sounds`, `voices`, `.github`, `*.atlas`, `*.png`,
   `preview.jpg`, `*.md`, `Atlases.json`, `.nomedia`, `license`) должно быть
   **больше**, чем остальных. Иначе — «Invalid Mod archive structure».
   → Любую новую dev-папку/файл в корне добавлять в `.gitattributes` как `export-ignore`.
3. Имя мода = имя репозитория, дефисы показываются как пробелы
   (`mod-Unciv` → «mod Unciv»). Допустимы только буквы, цифры и дефисы.
4. Кнопка «Обновить [мод]» появляется, когда на GitHub есть новая версия.

Топик `unciv-mod` на GitHub делает мод видимым во встроенном онлайн-списке
(для скачивания по ссылке не нужен). Категории: `unciv-mod-rulesets`,
`unciv-mod-expansions`, `unciv-mod-graphics`, `unciv-mod-audio`,
`unciv-mod-maps`, `unciv-mod-fun`, `unciv-mod-modsofmods`.

## 7. Установка на Android

**Основной способ (по ссылке):**
1. Unciv из Google Play / F-Droid / `Unciv-signed.apk` из релизов GitHub. Версия — свежая.
2. Главное меню → **«Моды»** → **«Скачать мод по URL»**.
3. Вставить `https://github.com/MASHINKA34/mod-Unciv` («Вставить из буфера обмена») → **«Скачать»**.
4. Расширение включается при старте: «Начать новую игру» → **«Моды-расширения»** → галочка.
   Аудиовизуальный мод: «Моды» → мод → «Перманентный аудиовизуальный мод».
5. Обновление: «Моды» → «Обновить [мод]», или удалить и скачать заново.

Репозиторий должен быть **публичным**, изменения — **запушены** в `main`.

**Вручную (не рекомендуется):** скопировать папку мода (внутри сразу `jsons/`,
`game.atlas` ...) в `Android/data/com.unciv.app/files/mods/<Имя-Мода>/`.
На Android 11+ файловые менеджеры туда не пускают — копировать с ПК по USB.
При старте игра переносит моды во внутреннюю папку.

## 8. Ссылки

- Моды, общее: https://yairm210.github.io/Unciv/Modders/Mods/
- Структура файлов: https://yairm210.github.io/Unciv/Modders/Mod-file-structure/1-Overview/
- Картинки и звук: https://yairm210.github.io/Unciv/Modders/Images-and-Audio/
- Новая цивилизация: https://yairm210.github.io/Unciv/Modders/Making-a-new-Civilization/
- Список uniques: https://yairm210.github.io/Unciv/Modders/uniques/
- Свой тайлсет: https://yairm210.github.io/Unciv/Modders/Creating-a-custom-tileset/
- JSON базовой игры: https://github.com/yairm210/Unciv/tree/master/android/assets/jsons/Civ%20V%20-%20Gods%20&%20Kings
- Примеры модов: [The-Undead](https://github.com/SpacedOutChicken/The-Undead) (компактная нация),
  [RekMOD](https://github.com/ravignir/RekMOD) (большой), [5Hex-Tileset](https://github.com/ravignir/5Hex-Tileset) (графика),
  все моды: https://github.com/topics/unciv-mod
