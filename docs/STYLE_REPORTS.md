# Оформление документов, которые агенты отдают людям

> Визуальная система принята владельцем продукта 16.10.2026. Ниже — исходный шаблон оформления в
> том виде, в каком он задан; ABOP применяет его ко ВСЕМ формам отчётов разом через общий
> `reports/base.css` и рендерер раскладки (`server/report_form.py`), а не копированием стилей в
> каждый бланк.
>
> **Что это значит на практике.** Палитра, шкала кеглей и компоненты живут токенами в `:root`:
> правка одного токена меняет все двадцать семь форм. Структурные требования шаблона (контейнер
> таблицы с прокруткой, `<thead>` для повторения шапки на печати, выноска вместо мелкого серого
> текста, верхняя строка «тема — вид документа») реализованы в рендерере, потому что это разметка,
> а не оформление. Инварианты закрыты тестами: `tests/test_report_css_tokens.py` (цвет только
> токеном, палитра компактна, шкала конечна, селектор объявлен один раз) и
> `tests/test_report_fits_page.py` (документ помещается в печатное поле A4).
>
> **Чего в ABOP намеренно нет из шаблона.** Декоративных плашек, которые можно принять за отметку
> о проверке, и иконок: в служебном документе значок рядом с суммой читается как статус, которого
> никто не присваивал. Ширина листа — 1080 px, как в шаблоне; «до 1180» не используется, чтобы
> экранный вид и PDF совпадали.

---

# Шаблон оформления HTML-документов

## Готовое задание для использования в промпте

Оформи документ в спокойном деловом стиле: тёплый светлый фон, тёмно-синий текст, бежевые карточки, тонкие рамки и умеренные скругления. Используй приложенный CSS и описанные ниже компоненты как основу. Стиль подходит для инструкций, аналитических материалов и отчётов.

Сохрани исходные данные, суммы, названия, оговорки и порядок разделов. Не сокращай содержание без отдельного указания. Весь текст и таблицы должны быть настоящей HTML-разметкой, доступной для выделения и копирования. Итог — один автономный HTML-файл с CSS внутри `<style>`, без внешних шрифтов, библиотек и обязательного подключения к интернету.

Не добавляй вымышленные показатели, статусы проверки или выводы. Декоративная плашка не должна выглядеть как подтверждение корректности отчёта. Не добавляй кнопки и навигацию, если они не нужны задаче.

## Визуальная система

| Элемент | Значение / правило |
|---|---|
| Фон за документом | `#eae6dc`, тёплый серо-бежевый |
| Основной фон документа | `#fcfaf4`, почти белый кремовый |
| Основной текст и заголовки | `#233746`, тёмный сине-серый |
| Вторичный текст | `#526b7c`, приглушённый синий |
| Акцент | `#506573`, сине-серый; маркеры, иконки, левая линия |
| Бежевые подложки | `#eee8da` |
| Светлая подложка карточек | `#f5f1e7` или `#f6f2e9` |
| Границы | `#d5cdbc`; внутренние линии таблиц `#e4ded1` |
| Тёмный итоговый блок | фон `#334855`, белый текст |
| Шрифт | `Arial, Helvetica, sans-serif`; без загрузки извне |
| Основной текст | 16 px, межстрочный интервал 1.55 |
| H1 | 48 px, интервал 1.08, letter-spacing −0.035em |
| H2 | 28 px, интервал 1.2, letter-spacing −0.02em |
| H3 | 20 px, интервал 1.3 |
| Вводный абзац | 20 px, вторичный цвет, интервал 1.5 |
| Таблицы | текст 14 px; заголовки 12 px |
| Ширина инструкции | максимум 1080 px, включая внутренние поля |
| Ширина сложного отчёта | допустимо увеличить до 1180 px |
| Поля документа | 48 px сверху/снизу, 54 px по бокам |
| Расстояние между разделами | 32 px |
| Карточки | рамка 1 px, скругление 12–16 px, поля 18–22 px |
| Тень документа | мягкая, только на экране |

## Правила композиции

1. **Шапка.** Слева — короткая надпись о теме документа с вертикальной линией. Справа — небольшая бежевая плашка с типом документа. Затем крупный H1 и вводный абзац. Название продукта или организации ставь только из исходных данных.
2. **Краткая схема.** Если помогает пониманию, под вводным абзацем размести три карточки шагов или результатов. В карточке — короткий жирный заголовок и одна поясняющая строка. На телефоне карточки располагаются друг под другом.
3. **Разделы.** Используй последовательную иерархию H1 → H2 → H3. Не оформляй каждый абзац отдельной карточкой. Обычный текст оставляй на основном светлом фоне.
4. **Карточки действий.** Для отдельных файлов, шагов или объектов используй `.export`. Номер — небольшой бежевый круг `.num`; имя файла — компактная моноширинная метка `.file`.
5. **Важные пояснения.** Используй `.callout`: бежевая подложка, тёмная вертикальная линия слева, без тревожных цветов. Выделяй конкретное действие или ограничение.
6. **Таблицы.** Бежевая шапка, тонкие горизонтальные разделители, умеренные отступы. Текст — по левому краю, суммы — по правому. Для чисел используй `font-variant-numeric: tabular-nums`. Не уменьшай шрифт ради помещения широкой таблицы на экран: разреши горизонтальную прокрутку внутри её контейнера.
7. **Итог.** Один важный вывод или границы ответственности можно поместить в тёмный блок `.footer`. Это визуальное выделение, а не обязательный футер каждой страницы.
8. **Иконки.** Только простые линейные SVG в сине-сером цвете, одинаковая толщина линий. Без эмодзи, фотографий, ярких градиентов и декоративных иллюстраций. Иконки не заменяют текстовые подписи.

## Дополнительные компоненты для отчётов

- Реквизиты: сетка из 2–3 колонок светлых карточек; подпись мелкая, значение крупнее. Длинные пути и идентификаторы переносить через `overflow-wrap: anywhere`.
- Показатели: сетка из 3–4 карточек. Подпись 10–12 px, значение 25–30 px. Один главный показатель можно выделить тёмной подложкой.
- Итоговые строки таблиц: бежевая заливка и полужирное начертание. Детальные строки остаются светлыми.
- Документы с большим количеством колонок: отдельный контейнер с прокруткой; перенос длинных ссылок. Не обрезать значения и не прятать колонки.
- Служебные сведения: меньший кегль, вторичный цвет; содержание сохранять. Не смешивать технические метаданные с основными выводами.

## Адаптивность, печать и проверка

- На экране до 700 px: убрать внешние поля и тень, уменьшить внутренние поля до 20 px, H1 до 36 px, H2 до 25 px. Три карточки преобразовать в одну колонку.
- Широкие таблицы прокручиваются внутри блока и не расширяют всю страницу.
- Для печати: A4, поля 12–14 мм, без внешней тени; основной текст около 10 pt. Печать может занимать несколько страниц: не пытайся уместить длинный документ на один лист уменьшением шрифта.
- Использовать `<thead>` для повторения шапки таблицы. По возможности не разрывать строки таблиц и небольшие карточки между страницами. Не запрещать разрыв целой длинной таблицы.
- Перед выдачей проверить вид на широком и узком экранах и в предпросмотре печати: отсутствие наложений, обрезанных сумм и ссылок, читаемость таблиц, сохранность содержания.

## Базовый CSS

Это CSS из оформленной инструкции. Его можно использовать без изменений, а затем добавлять необходимые для конкретного отчёта компоненты. Цвета задавай через переменные в `:root`.

```css
*{box-sizing:border-box} :root{--ink:#233746;--muted:#526b7c;--paper:#fcfaf4;--sand:#eee8da;--line:#d5cdbc;--accent:#506573}body{margin:0;background:#eae6dc;color:var(--ink);font:16px/1.55 Arial,Helvetica,sans-serif}.sheet{max-width:1080px;margin:28px auto;background:var(--paper);padding:48px 54px;box-shadow:0 10px 40px #293b4618}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}.brand{border-left:5px solid var(--accent);padding-left:14px}.pill{background:var(--sand);border-radius:30px;padding:8px 17px;white-space:nowrap}h1{font-size:48px;line-height:1.08;letter-spacing:-.035em;margin:28px 0 18px;max-width:780px}h2{font-size:28px;line-height:1.2;letter-spacing:-.02em;margin:0 0 18px}h3{font-size:20px;line-height:1.3;margin:0 0 10px}p{margin:10px 0}.lead{font-size:20px;line-height:1.5;color:var(--muted);max-width:820px}.flow{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:26px 0 32px}.tile{border:1px solid var(--line);border-radius:15px;padding:18px;background:#f5f1e7}.tile span{display:block;color:var(--muted);font-size:14px;margin-top:4px}.tile b{font-size:18px}.file{font:700 13px/1.3 monospace;color:var(--muted);background:var(--sand);padding:5px 9px;border-radius:7px;display:inline-block;margin-bottom:12px}section{margin-top:32px}.export{margin:16px 0;padding:22px;border:1px solid var(--line);border-radius:16px;background:#fffdf8}.num{display:inline-flex;width:30px;height:30px;align-items:center;justify-content:center;border-radius:50%;background:var(--sand);margin-right:9px;font-size:16px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;margin:16px 0}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;padding:11px 13px;border-bottom:1px solid #e4ded1}th{background:var(--sand);font-size:12px;line-height:1.4}td:first-child{font-weight:700}tr:last-child td{border-bottom:0}tbody tr:last-child.total{background:var(--sand)}.callout{background:var(--sand);border-radius:12px;padding:17px 20px;margin-top:18px;border-left:4px solid var(--accent)}.results{display:grid;gap:12px}.result{display:flex;gap:14px;padding:18px;background:#f3efe5;border-radius:13px}.icon{flex:none;width:38px;height:42px;color:var(--accent)}.result p{margin:0}.result b{display:block;margin-bottom:4px}.stat{display:flex;gap:22px;align-items:center;padding:19px 23px;background:var(--sand);border-radius:14px;margin:16px 0}.stat strong{font-size:38px;line-height:1;white-space:nowrap}.stat span{color:var(--muted)}.numbers td:not(:first-child){font-variant-numeric:tabular-nums;white-space:nowrap}.footer{padding:23px 25px;margin-top:30px;background:#334855;color:#fff;border-radius:15px;font-size:16px}.footer p{margin:0}@media(max-width:700px){.sheet{margin:0;padding:26px 20px}h1{font-size:36px}h2{font-size:25px}.flow{grid-template-columns:1fr}.top{font-size:10px}.export{padding:17px}.lead{font-size:18px}.numbers{min-width:580px}table{min-width:500px}.stat strong{font-size:30px}}@media print{@page{size:A4;margin:12mm}body{background:white}.sheet{margin:0;padding:0;max-width:none;box-shadow:none;font-size:10pt}h1{font-size:30pt}h2{font-size:19pt}h3{font-size:14pt}.lead{font-size:12pt}table{font-size:9pt}th,td{padding:7px 9px}.flow{margin:18px 0}.tile{padding:12px}section{margin-top:22px}tr,.export,.result,.footer,.callout{break-inside:avoid}thead{display:table-header-group}h2,h3{break-after:avoid}.table-wrap{overflow:visible}.sheet *{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
/* Дополнение: числовые колонки выравниваются вправо. */
th.amount, td.amount { text-align: right; white-space: nowrap; }
```

## Минимальная HTML-разметка

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Название документа</title>
  <style>/* Вставить базовый CSS из шаблона */</style>
</head>
<body>
  <main class="sheet">
    <header>
      <div class="top">
        <div class="brand">Тема документа</div>
        <div class="pill">Инструкция / Отчёт</div>
      </div>
      <h1>Название документа</h1>
      <p class="lead">Кому адресован документ, что нужно сделать и какой результат ожидается.</p>
    </header>

    <section>
      <h2>Название раздела</h2>
      <article class="export">
        <h3><span class="num">1</span>Название шага</h3>
        <div class="file">имя_файла.csv</div>
        <p>Описание действия.</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th scope="col">Показатель</th><th scope="col" class="amount">Сумма</th></tr></thead>
            <tbody><tr><td>Название показателя</td><td class="amount">1 000 000</td></tr></tbody>
          </table>
        </div>
        <div class="callout"><b>Важно:</b> конкретное условие или действие.</div>
      </article>
    </section>

    <footer class="footer"><p>Главный вывод или границы ответственности.</p></footer>
  </main>
</body>
</html>
```
