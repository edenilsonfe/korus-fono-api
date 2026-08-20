BEGIN TRANSACTION READ ONLY;

WITH demo AS (
    SELECT id
    FROM patients
    WHERE is_demo IS TRUE
)
SELECT
    COUNT(*) AS demo_patients,
    COUNT(*) FILTER (
        WHERE (
            SELECT COUNT(DISTINCT entry.section)
            FROM anamnese_entries AS entry
            WHERE entry.patient_id = demo.id
              AND entry.section = ANY (ARRAY[
                  'Gestação',
                  'Parto',
                  'Desenvolvimento motor',
                  'Desenvolvimento da linguagem',
                  'Histórico escolar',
                  'Comorbidades',
                  'Medicamentos',
                  'Observações'
              ])
        ) = 8
    ) AS with_complete_demo_anamnesis,
    COUNT(*) FILTER (
        WHERE (
            SELECT COUNT(DISTINCT evolution.title)
            FROM evolutions AS evolution
            WHERE evolution.patient_id = demo.id
              AND evolution.title = ANY (ARRAY[
                  'Avaliação inicial',
                  'Adaptação ao processo terapêutico',
                  'Ampliação da comunicação funcional',
                  'Evolução recente'
              ])
        ) = 4
    ) AS with_four_demo_evolutions,
    COUNT(*) FILTER (
        WHERE (
            SELECT COUNT(DISTINCT assessment.protocol_id)
            FROM assessments AS assessment
            WHERE assessment.patient_id = demo.id
              AND assessment.metadata ->> 'source' = 'korus_demo_history'
        ) = 2
    ) AS with_two_demo_assessments,
    COUNT(*) FILTER (
        WHERE (
            SELECT COUNT(DISTINCT goal.title)
            FROM goals AS goal
            WHERE goal.patient_id = demo.id
              AND goal.title = ANY (ARRAY[
                  'Ampliar vocabulário funcional',
                  'Combinar duas palavras espontaneamente'
              ])
        ) = 2
    ) AS with_two_demo_goals,
    COUNT(*) FILTER (
        WHERE (
            SELECT COUNT(*)
            FROM clinical_domain_snapshots AS snapshot
            WHERE snapshot.patient_id = demo.id
              AND snapshot.key = ANY (ARRAY['linguagem', 'social', 'atencao'])
        ) >= 9
    ) AS with_domain_history
FROM demo;

SELECT
    COUNT(*) FILTER (WHERE patient.is_demo IS TRUE) AS demo_seeded_assessments,
    COUNT(*) FILTER (WHERE patient.is_demo IS FALSE) AS real_seeded_assessments,
    COUNT(*) FILTER (WHERE assessment.protocol_id = 'mchat') AS seeded_mchat_assessments
FROM assessments AS assessment
JOIN patients AS patient ON patient.id = assessment.patient_id
WHERE assessment.metadata ->> 'source' = 'korus_demo_history';

ROLLBACK;
