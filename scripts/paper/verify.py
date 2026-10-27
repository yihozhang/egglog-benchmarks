#!/usr/bin/env python3
"""Verify final paper artifacts without rerunning timed experiments."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness import artifacts, results

EXPECTED = ['scaling', 'scaling-real-world', 'e2e', 'storel', 'scaling-storel',
            'storel-schedule', 'ablation', 'ablation-storel', 'engines', 'query']
ALGEBRAIC = {
    ('math-microbenchmark', 'math-microbenchmark'),
    ('math-microbenchmark', 'math-microbenchmark-12'),
    ('horner', 'horner-9'), ('horner', 'horner-10'),
    ('matmul-assoc', 'matmul-assoc-320'),
    ('matmul-assoc', 'matmul-assoc-480'),
    ('matmul-assoc', 'matmul-assoc-640'),
}
SCALING = {
    ('math-microbenchmark', 'math-microbenchmark'),
    ('horner', 'horner-9'), ('matmul-assoc', 'matmul-assoc-320'),
}
FIGURES = ['engines', 'real-world', 'storel-backoff', 'scaling-algebraic',
           'scaling-real-world', 'storel-schedule', 'storel-schedule-work',
           'ablation', 'ablation-refinement', 'query-totals', 'query-scatter',
           'real-world-details', 'scaling-real-world-details',
           'ablation-details', 'ablation-refinement-details']
FIGURES += ['engines-' + benchmark for _, benchmark in sorted(ALGEBRAIC)]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

progress = json.loads((ROOT / 'results/paper/progress.json').read_text())
base = 'c1e12aef4907f8ab0febb3568c1e6bae462051b5'
verified = {}
for name in EXPECTED:
    entry = progress['experiments'][name]
    assert entry['state'] == 'complete', (name, entry)
    path = ROOT / 'runs' / entry['run_id']
    manifest = artifacts.verify_run(path)
    selection, _ = results.read_selection(name, progress['selection'])
    if name == 'query':
        assert manifest['config']['no_decomp_only'] is True
        assert manifest['config']['egglog_ref'] == base
        assert manifest['egglog_commit'] == base
        assert manifest['measurement']['egglog']['flags'] == ['--no-decomp']
        for program in manifest['programs']:
            records = [json.loads(line) for line in (path / program / 'egglog.jsonl').read_text().splitlines()]
            assert records, program
            assert all(r['no_decomp'] is True and r['engine_label'] == 'egglog-cracked' for r in records), program
    else:
        rows = [json.loads(line) for line in (path / 'measurements.jsonl').read_text().splitlines()]
        for row in rows:
            if row['system'].startswith('egglog') and row['variant'] != 'egglog-prior':
                assert row['commit'] == base
                assert '--no-decomp' in row['flags']
            if row['suite'] == 'storel':
                assert row['variant'] != 'egglog-prior'
            if row['variant'].startswith('no-DVO'):
                assert '--no-dynamic-order' in row['flags']
                assert ('--static-order-refinement' in row['flags']) == (row['variant'] == 'no-DVO-refinement')
        if name in ('engines', 'ablation'):
            algebraic = {(r['suite'], r['benchmark']) for r in rows
                         if r['suite'] in {s for s, _ in ALGEBRAIC}}
            assert algebraic == ALGEBRAIC, (name, algebraic)
        if name == 'engines':
            rerun = json.loads((ROOT / 'results/paper/engine-rerun.json').read_text())
            assert entry['run_id'] == rerun['replacement_run_id']
            assert entry['run_id'] != rerun['excluded_run_id']
            for system in ('hegg', 'metatheory', 'ego', 'foresight', 'ggs'):
                adapter = manifest['builds'][system]['adapter_snapshots'][f'adapters/{system}/run']
                assert adapter['sha256'] == rerun['adapter_sha256_after'], system
        if name == 'scaling':
            configurations = {(r['suite'], r['benchmark'], r['variant'], r['threads']) for r in rows}
            expected = {(suite, benchmark, variant, threads)
                        for suite, benchmark in SCALING
                        for variant in ('egglog-cracked', 'foresight')
                        for threads in (1, 2, 4, 8, 16)}
            assert configurations == expected, (name, configurations ^ expected)
            assert all(r['status'] == 'ok' for r in rows), 'Scaling sizes must finish for both engines'
        if name in ('ablation', 'ablation-storel'):
            assert {r['variant'] for r in rows} == {'no-DVO', 'no-DVO-refinement', 'egglog-cracked'}
            assert {tuple(r['patch_hashes']) for r in rows} == {(digest(ROOT/'systems/egglog/patches/no-dynamic-order.patch'),)}
    verified[name] = {'run_id': entry['run_id'], 'check_exit_code': entry['check_exit_code']}

figure_dir = ROOT / 'figures/paper'
manifest = json.loads((figure_dir / 'manifest.json').read_text())
assert digest(ROOT / 'scripts/paper/plot.py') == manifest['plot_script_sha256']
for name, sha in manifest['render_dependencies_sha256'].items():
    assert digest(ROOT / name) == sha, name
for name, sha in manifest['files'].items():
    assert digest(figure_dir / name) == sha, name
assert json.loads((ROOT / 'results/paper/figures.json').read_text()) == manifest
for name in FIGURES:
    assert (figure_dir / f'{name}.pdf').is_file(), name
    assert (figure_dir / f'{name}.png').is_file(), name
texts = {}
for path in sorted(figure_dir.glob('*.pdf')):
    content = subprocess.check_output(['pdftotext', str(path), '-'], text=True)
    # Match engine/mode labels; cholesky_decomp is a legitimate eggcc workload.
    assert not re.search(r'\b(?:decomp(?:osition)?|egglog_[nt]d|TD)\b', content, re.I), path.name
    texts[path.name] = len(content)
assert 'egglog-cracked' in subprocess.check_output(['pdftotext', str(figure_dir/'engines.pdf'), '-'], text=True)
assert 'egglog-prior' in subprocess.check_output(['pdftotext', str(figure_dir/'engines.pdf'), '-'], text=True)
report = {'experiments': verified, 'verifier_sha256': digest(Path(__file__)),
          'figure_checksums_verified': len(manifest['files']),
          'pdf_labels_verified': texts,
          'note': 'This verifies provenance and settings. Nonzero size-check exits remain findings, not passes.'}
(ROOT / 'results/paper/final-verification.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
