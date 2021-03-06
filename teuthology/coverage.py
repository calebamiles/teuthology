import argparse
from contextlib import closing
import logging
import os
import shutil
import subprocess
import MySQLdb
import yaml

from teuthology import misc as teuthology

log = logging.getLogger(__name__)

def connect_to_db(ctx):
    db = MySQLdb.connect(
        host=ctx.teuthology_config['coverage_db_host'],
        user=ctx.teuthology_config['coverage_db_user'],
        db=ctx.teuthology_config['coverage_db_name'],
        passwd=ctx.teuthology_config['coverage_db_password'],
        )
    db.autocommit(False)
    return db

def store_coverage(ctx, test_coverage, rev, suite):
    with closing(connect_to_db(ctx)) as db:
        rows = []
        for test, coverage in test_coverage.iteritems():
            flattened_cov = [item for sublist in coverage for item in sublist]
            rows.append([rev, test, suite] + flattened_cov)
        log.debug('inserting rows into db: %s', str(rows))
        try:
            cursor = db.cursor()
            cursor.executemany(
                'INSERT INTO `coverage`'
                ' (`rev`, `test`, `suite`, `lines`, `line_cov`, `functions`,'
                ' `function_cov`, `branches`, `branch_cov`)'
                ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                rows)
        except:
            log.exception('error updating database')
            db.rollback()
            raise
        else:
            db.commit()
            log.info('added coverage to database')
        finally:
            cursor.close()

def read_coverage(output):
    log.debug('reading coverage from output: %s', output)
    coverage = [None, None, None]
    prefixes = ['  lines......: ', '  functions..: ', '  branches...: ']
    for line in reversed(output.splitlines()):
        for i, prefix in enumerate(prefixes):
            if line.startswith(prefix):
                if '%' in line:
                    cov_num = int(line[line.find('(') + 1:line.find(' of')])
                    cov_percent = float(line[len(prefix):line.find('%')])
                    coverage[i] = (cov_num, cov_percent)
                else:
                    # may have no data for e.g. branches on the initial run
                    coverage[i] = (None, None)
                break
        if None not in coverage:
            break
    return coverage

def analyze():
    parser = argparse.ArgumentParser(description="""
Analyze the coverage of a suite of test runs, generating html output with lcov.
""")
    parser.add_argument(
        '-o', '--lcov-output',
        help='the directory in which to store results',
        required=True,
        )
    parser.add_argument(
        '--html-output',
        help='the directory in which to store html output',
        )
    parser.add_argument(
        '--cov-tools-dir',
        help='the location of coverage scripts (cov-init and cov-analyze)',
        default='../../coverage',
        )
    parser.add_argument(
        '--skip-init',
        help='skip initialization (useful if a run stopped partway through)',
        action='store_true',
        default=False,
        )
    parser.add_argument(
        '-v', '--verbose',
        help='be more verbose',
        action='store_true',
        default=False,
        )
    parser.add_argument(
        'test_dir',
        help='the location of the test results',
        )
    args = parser.parse_args()

    loglevel = logging.INFO
    if args.verbose:
        loglevel = logging.DEBUG

    logging.basicConfig(
        level=loglevel,
        )

    teuthology.read_config(args)

    handler = logging.FileHandler(
        filename=os.path.join(args.test_dir, 'coverage.log'),
        )
    formatter = logging.Formatter(
        fmt='%(asctime)s.%(msecs)03d %(levelname)s:%(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
        )
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)

    try:
        _analyze(args)
    except:
        log.exception('error generating coverage')
        raise

def _analyze(args):
    tests = [
        f for f in sorted(os.listdir(args.test_dir))
        if not f.startswith('.')
        and os.path.isdir(os.path.join(args.test_dir, f))
        and os.path.exists(os.path.join(args.test_dir, f, 'summary.yaml'))
        and os.path.exists(os.path.join(args.test_dir, f, 'ceph-sha1'))]

    test_summaries = {}
    for test in tests:
        summary = {}
        with file(os.path.join(args.test_dir, test, 'summary.yaml')) as f:
            g = yaml.safe_load_all(f)
            for new in g:
                summary.update(new)

        if summary['flavor'] != 'gcov':
            log.debug('Skipping %s, since it does not include coverage', test)
            continue
        test_summaries[test] = summary

    assert len(test_summaries) > 0

    suite = os.path.basename(args.test_dir)

    # only run cov-init once.
    # this only works if all tests were run against the same version.
    if not args.skip_init:
        log.info('initializing coverage data...')
        subprocess.check_call(
            args=[
                os.path.join(args.cov_tools_dir, 'cov-init.sh'),
                os.path.join(args.test_dir, tests[0]),
                args.lcov_output,
                os.path.join(
                    args.teuthology_config['ceph_build_output_dir'],
                    '{suite}.tgz'.format(suite=suite),
                    ),
                ])
        shutil.copy(
            os.path.join(args.lcov_output, 'base.lcov'),
            os.path.join(args.lcov_output, 'total.lcov')
            )

    test_coverage = {}
    for test, summary in test_summaries.iteritems():
        lcov_file = '{name}.lcov'.format(name=test)

        log.info('analyzing coverage for %s', test)
        proc = subprocess.Popen(
            args=[
                os.path.join(args.cov_tools_dir, 'cov-analyze.sh'),
                '-t', os.path.join(args.test_dir, test),
                '-d', args.lcov_output,
                '-o', test,
                ],
            stdout=subprocess.PIPE,
            )
        output, _ = proc.communicate()
        desc = summary.get('description', test)
        test_coverage[desc] = read_coverage(output)

        log.info('adding %s data to total', test)
        proc = subprocess.Popen(
            args=[
                'lcov',
                '-a', os.path.join(args.lcov_output, lcov_file),
                '-a', os.path.join(args.lcov_output, 'total.lcov'),
                '-o', os.path.join(args.lcov_output, 'total_tmp.lcov'),
                ],
            stdout=subprocess.PIPE,
            )
        output, _ = proc.communicate()

        os.rename(
            os.path.join(args.lcov_output, 'total_tmp.lcov'),
            os.path.join(args.lcov_output, 'total.lcov')
            )

    coverage = read_coverage(output)
    test_coverage['total for {suite}'.format(suite=suite)] = coverage
    log.debug('total coverage is %s', str(coverage))

    if args.html_output:
        subprocess.check_call(
            args=[
                'genhtml',
                '-s',
                '-o', os.path.join(args.html_output, 'total'),
                '-t', 'Total for {suite}'.format(suite=suite),
                '--',
                os.path.join(args.lcov_output, 'total.lcov'),
                ])

    store_coverage(args, test_coverage, summary['ceph-sha1'], suite)
