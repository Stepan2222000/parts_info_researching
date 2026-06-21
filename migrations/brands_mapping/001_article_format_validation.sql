-- brands_mapping DB (194.164.245.107:5411) — расширение под формат-валидацию артикулов
-- для parts_research (research/curator). ОБЩАЯ БД: её также читают ebay_orders/matching.py
-- (ungated finditer по титулам) и ebay_validation_item/evi (gated по бренду эталона).
--
-- ВНИМАНИЕ: brands_mapping без своей системы миграций — применяется ВРУЧНУЮ один раз.
-- Этот файл — durable-запись. ЧЕРНОВИК НА РЕВЬЮ: не применять, пока не согласовано
-- (изменения видны eBay-системам).
--
-- Принципы:
--   * from→to записан в самом find_regex: regex матчит грязную форму, capture-группа =
--     каноника, всё незахваченное (дилерский префикс NN-, пробел) отбрасывается;
--   * правки существующих строк — capture-нейтральны (проверено: кандидат eBay не меняется);
--   * новые правила — узкие, brand-scoped (минимум ложных кандидатов в ungated eBay-матчинге).

BEGIN;

-- 1) Колонки-документация from→to (eBay их не читает; чисто наглядность). ------------
ALTER TABLE article_match_rules ADD COLUMN IF NOT EXISTS example_from text;
ALTER TABLE article_match_rules ADD COLUMN IF NOT EXISTS example_to   text;

-- 2) Унификация среза дилерского префикса у Mercury-правил, где его не было. ----------
--    (?:\d{1,3}-)? — non-capturing, capture-группа не меняется → eBay-кандидат тот же.
UPDATE article_match_rules
   SET find_regex = '(?<![A-Z0-9])(?:\d{1,3}-)?(8M\d{7})(?![A-Z0-9])',
       note = note || ' Дилерский префикс NN- срезается (вне группы).'
 WHERE name = 'mercury_8m';

UPDATE article_match_rules
   SET find_regex = '(?<![A-Z0-9])(?:\d{1,3}-)?(\d{6,7}[A-Z]{2})(?![A-Z0-9])',
       note = note || ' Дилерский префикс NN- срезается (вне группы).'
 WHERE name = 'mercury_alpha2';

-- 3) Примеры from→to для существующих правил (наглядность). --------------------------
UPDATE article_match_rules SET example_from='26-8M0204670', example_to='8M0204670'  WHERE name='mercury_8m';
UPDATE article_match_rules SET example_from='26-76868',     example_to='76868'      WHERE name='numeric_4_9';
UPDATE article_match_rules SET example_from='26-879884T',   example_to='879884T'    WHERE name='mercury_alnum';
UPDATE article_match_rules SET example_from='5010181JP',    example_to='5010181JP'  WHERE name='mercury_alpha2';
UPDATE article_match_rules SET example_from='26-88397A 1',  example_to='88397A1'    WHERE name='mercury_space_merge';
UPDATE article_match_rules SET example_from='09-812B',      example_to='09-812B'    WHERE name='volvo_dashed';
UPDATE article_match_rules SET example_from='876266-8',     example_to='876266'     WHERE name='volvo_rev_suffix';
UPDATE article_match_rules SET example_from='SSC13416',     example_to='SSC13416'   WHERE name='seastar_alpha';
UPDATE article_match_rules SET example_from='17400-90J11',  example_to='17400-90J11' WHERE name='suzuki_seg';
UPDATE article_match_rules SET example_from='17400-92823',  example_to='17400-92823' WHERE name='suzuki_5_5';
UPDATE article_match_rules SET example_from='06192-ZW9-000',example_to='06192-ZW9-000' WHERE name='honda_3seg';
UPDATE article_match_rules SET example_from='1019520-067',  example_to='1019520-067' WHERE name='polaris_dashed';
UPDATE article_match_rules SET example_from='4X7-13440-90', example_to='4X7-13440-90' WHERE name='yamaha_seg';
UPDATE article_match_rules SET example_from='007-626',      example_to='007-626'    WHERE name='brp_dashed';

-- 4) Недостающие бренды (в smart есть, правил не было вовсе). -------------------------
--    Каноника = как хранит smart (без дилерских префиксов у этих брендов).
INSERT INTO article_match_rules (name, canonical, find_regex, note, enabled, example_from, example_to) VALUES
 ('mercedes_a', 'MERCEDES_BENZ',
  '(?<![A-Z0-9])((?:ZB)?A\d{10}(?:\d{2})?)(?![A-Z0-9])',
  'Mercedes: (опц. ZB) + A + 10 или 12 цифр (A0009054308, A000905740564, ZBA0009052504). Каноника = весь матч.',
  true, 'A0009054308', 'A0009054308'),

 ('audi_zone', 'AUDI',
  '(?<![A-Z0-9])([0-9][A-Z][0-9]\d{6}[A-Z]?)(?![A-Z0-9])',
  'Audi/VW зонный: цифра+буква+цифра + 6 цифр + опц. буква (4M0820021, 4M1820021A).',
  true, '4M1820021A', '4M1820021A'),
 ('audi_pr', 'AUDI',
  '(?<![A-Z0-9])([A-Z]{3}\d{6,8}[A-Z]?)(?![A-Z0-9])',
  'Audi/VW буквенный: 3 буквы + 6-8 цифр + опц. буква (PAB820021, PAB82002100).',
  true, 'PAB820021', 'PAB820021'),

 ('arctic_dashed', 'ARCTIC_CAT',
  '(?<![A-Z0-9])(\d{4}-\d{3})(?![A-Z0-9])',
  'Arctic Cat дефисный: NNNN-NNN (0823-496).',
  true, '0823-496', '0823-496'),

 ('land_rover_alpha', 'LAND_ROVER',
  '(?<![A-Z0-9])([A-Z]{2}\d{6})(?![A-Z0-9])',
  'Land Rover: 2 буквы + 6 цифр (LR011710).',
  true, 'LR011710', 'LR011710');

-- 5) Per-brand числовые/буквенно-цифровые формы (закрывают ~24% NO_RULE на draft'ах). --
--    ВАЖНО про eBay: matching.py гоняет правила UNGATED по титулам. Числовые/alnum формы
--    eBay УЖЕ извлекает существующими numeric_4_9/mercury_alnum -> кандидаты совпадают,
--    set дедупит -> для 4-9-значных НЕЙТРАЛЬНО. Новое для eBay: 10-значные (BRP/Volvo) и
--    alnum-формы брендов -> РЕВЬЮ на eBay-матчинге перед apply (особенно [*] широкие).
INSERT INTO article_match_rules (name, canonical, find_regex, note, enabled, example_from, example_to) VALUES
 ('volvo_numeric',  'VOLVO',   '(?<![A-Z0-9])(\d{4,10})(?![A-Z0-9-])', 'Volvo чисто-числовой 4-10 цифр (854684, 3854260).', true, '854684','854684'),
 ('volvo_alnum',    'VOLVO',   '(?<![A-Z0-9])(\d{3,7}[A-Z]\d{0,2})(?![A-Z0-9])', 'Volvo цифры+буква+0-2 цифры (811635T, 811635T3, 898253T22).', true, '811635T3','811635T3'),

 ('suzuki_numeric', 'SUZUKI',  '(?<![A-Z0-9])(\d{4,7})(?![A-Z0-9-])', 'Suzuki чисто-числовой (778887, 5033919).', true, '778887','778887'),

 ('honda_numeric',  'HONDA',   '(?<![A-Z0-9])(\d{4,8})(?![A-Z0-9-])', 'Honda чисто-числовой (816464, 8164641).', true, '816464','816464'),

 ('brp_numeric',    'BRP',     '(?<![A-Z0-9])(\d{6,10})(?:KIT)?(?![A-Z0-9-])', 'BRP чисто-числовой 6-10 цифр, опц. суффикс KIT (331236451, 420611397KIT).', true, '420611397KIT','420611397'),
 -- [*] BRP alnum широковат — ревью на eBay
 ('brp_alnum',      'BRP',     '(?<![A-Z0-9])(\d{4,7}[A-Z]\d{2,6})(?![A-Z0-9])', 'BRP цифры+буква+цифры (40031M09300).', true, '40031M09300','40031M09300'),

 ('polaris_numeric','POLARIS', '(?<![A-Z0-9])(\d{6,7})(?![A-Z0-9-])', 'Polaris чисто-числовой 6-7 цифр (2410615, 1521172).', true, '2410615','2410615'),
 ('polaris_alnum',  'POLARIS', '(?<![A-Z0-9])(\d{4,7}[A-Z])(?![A-Z0-9])', 'Polaris цифры+буква (50083T).', true, '50083T','50083T'),
 ('polaris_na',     'POLARIS', '(?<![A-Z0-9])NA-(\d{4,7}[A-Z]?)(?![A-Z0-9])', 'Polaris с префиксом NA- (NA-50083T -> 50083T).', true, 'NA-50083T','50083T'),

 ('omc_numeric',    'OMC',     '(?<![A-Z0-9])(\d{4,9})(?![A-Z0-9-])', 'OMC чисто-числовой (984222).', true, '984222','984222'),

 ('yamaha_numeric', 'YAMAHA',  '(?<![A-Z0-9])(\d{4,8})(?![A-Z0-9-])', 'Yamaha чисто-числовой (824853).', true, '824853','824853'),
 -- [*] Yamaha alnum (модель-серия) широковат — ревью на eBay
 ('yamaha_alnum',   'YAMAHA',  '(?<![A-Z0-9])([0-9][A-Z0-9]{2}\d{7})(?![A-Z0-9])', 'Yamaha серия: 3 alnum + 7 цифр (62Y1241400, 65W1241400).', true, '62Y1241400','62Y1241400'),

 ('seastar_seg',    'SEASTAR', '(?<![A-Z0-9])([A-Z]{2,4}\d{3}-\d{2})(?![A-Z0-9])', 'SeaStar сегментный (SSC134-16).', true, 'SSC134-16','SSC134-16'),
 ('seastar_dashed', 'SEASTAR', '(?<![A-Z0-9])(\d{4}-\d{4})(?![A-Z0-9])', 'SeaStar дефисный (0324-3608).', true, '0324-3608','0324-3608');

COMMIT;

-- Остаток (~4% NO_RULE: Mercury F-/QB1-/суффиксы, Volvo 09-..-варианты, Suzuki -MHL/3-seg,
-- SeaStar слитные, aftermarket MAR-/KN-/HF-, parts без brand_oem) дозаполняется ПОЗЖЕ по
-- реальному бэклогу из article_format_problems (soft-прогон) — с твоим ревью.
