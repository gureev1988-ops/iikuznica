import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

const here = (name) => fileURLToPath(new URL(name, import.meta.url));
const passages = JSON.parse(execFileSync('python3', [here('extract.py')], { maxBuffer: 8_000_000 })).filter(p => p.text !== p.section);
const questions = readFileSync(here('questions.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
const glossary = new Map();
for (const line of readFileSync(here('../../corpus/glossary.md'), 'utf8').split('\n')) {
  const match = line.match(/^\| ([^|]+) \| ([^|]+) \|$/);
  if (!match || match[1] === 'Сокращение') continue;
  for (const abbr of match[1].split(', ')) glossary.set(abbr, match[2]);
}

// Preserve short uppercase abbreviations such as ПО that are also Russian stop words.
function encodeText(text) {
  return text.replace(/[\p{L}\d-]+/gu, word => glossary.has(word) ? word + ' аббр' + word : word)
    .toLowerCase().replaceAll('ё', 'е');
}

function directorates(text) {
  const docs = new Set();
  if (/дбрцт|департамент[а-я]* бизнес-решений/iu.test(text)) docs.add('DB');
  if (/(?:^|[^а-яё])(?:иб|диб)(?:$|[^а-яё])|информационн[а-я]* безопасност/iu.test(text)) {
    docs.add('IB');
    docs.add('PRE');
  }
  if (/тех(?:ническ[а-я]*)?\.?\s+дирекци/iu.test(text)) docs.add('TECH');
  return Array.from(docs);
}

const db = new PGlite();
try {
  await db.exec(`
    CREATE TABLE passage (idx int PRIMARY KEY, doc text, body tsvector, heading tsvector);
  `);
  await db.query(`
    INSERT INTO passage
    SELECT idx, doc, to_tsvector('russian', body), to_tsvector('russian', heading)
    FROM jsonb_to_recordset($1::jsonb) AS p(idx int, doc text, body text, heading text)
  `, [JSON.stringify(passages.map((p, idx) => ({
    idx, doc: p.doc, body: encodeText(p.text), heading: encodeText(p.section)
  })))]);
  // One row per (passage, lexeme). IDF uses the whole corpus before directory filtering.
  await db.exec(`
    CREATE TEMP TABLE posting AS
    SELECT p.idx, t.lexeme, cardinality(t.positions) AS tf, p.heading @@
           plainto_tsquery('simple', t.lexeme) AS in_heading
    FROM passage p, LATERAL unnest(p.body || p.heading) t;
    CREATE INDEX ON posting (lexeme);
    CREATE TEMP TABLE term_stats AS SELECT lexeme, ln(1.0 +
      ((SELECT count(*) FROM passage) - count(*) + 0.5) / (count(*) + 0.5)) AS idf
    FROM posting GROUP BY lexeme;
    CREATE TEMP TABLE lengths AS SELECT idx, sum(tf) AS n FROM posting GROUP BY idx;
  `);
  const cases = [];
  for (const { id, question } of questions) {
    const words = question.match(/[\p{L}\d-]+/gu) || [];
    const expanded = words.filter(w => glossary.has(w)).map(w => glossary.get(w)).join(' ');
    const docs = directorates(question);
    // BM25 over PostgreSQL's Russian lexemes, with lighter glossary and heading terms.
    const result = await db.query(`
      WITH tokens AS (
        SELECT token, max(weight) AS weight FROM (
          SELECT unnest(tsvector_to_array(to_tsvector('russian',$1))) AS token, 1.0 AS weight
          UNION ALL SELECT unnest(tsvector_to_array(to_tsvector('russian',$2))), 0.35
        ) AS q GROUP BY token
      ), average AS (SELECT avg(n) AS len FROM lengths)
      SELECT p.idx, sum(q.weight * s.idf * t.tf * 2.2 /
        (t.tf + 1.2 * (0.25 + 0.75 * l.n / average.len)) *
        (CASE WHEN t.in_heading THEN 1.35 ELSE 1 END)) AS score
      FROM tokens q JOIN posting t ON t.lexeme = q.token
      JOIN term_stats s USING (lexeme) JOIN passage p USING (idx) JOIN lengths l USING (idx)
      CROSS JOIN average
      WHERE cardinality($3::text[]) = 0 OR p.doc = ANY($3)
      GROUP BY p.idx ORDER BY score DESC, p.idx LIMIT 5
    `, [encodeText(question), encodeText(expanded), docs]);
    cases.push({ id, question, sources: result.rows.map(row => passages[row.idx]) });
  }
  writeFileSync(here('contexts.jsonl'), cases.map(x => JSON.stringify(x)).join('\n') + '\n');
  console.log(`Indexed ${passages.length} passages from 4 regulations. Retrieved sources for ${cases.length} questions.`);
} finally {
  await db.close();
}
