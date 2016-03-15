"""
Reindexes indexes to save space, but does it in a non-locking manner.

This recovers space from index bloat.
"""
from __future__ import division
import argparse
import psycopg2
import psycopg2.extras
import math
import sys
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def make_indexdef_concurrent(indexdef):
    """Turn an index creation statement into a concurrent index creationstatement."""
    if indexdef.startswith("CREATE INDEX "):
        indexdef = indexdef.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)
    elif indexdef.startswith("CREATE UNIQUE INDEX "):
        indexdef = indexdef.replace("CREATE UNQUE INDEX ", "CREATE UNQUE INDEX CONCURRENTLY ", 1)
    else:
        raise ValueError("Unknown index creation: {}".format(indexdef))

    return indexdef


def index_size(cursor, iname):
    cursor.execute("select pg_relation_size(pg_class.oid) FROM pg_class WHERE relname = %s;", (iname,))
    size = cursor.fetchone()[0]
    return size


def does_index_exist(cursor, iname):
    cursor.execute("select 1 from pg_indexes where schemaname = 'public' and indexname = %s limit 1;", (iname,))
    result = cursor.fetchone()
    return result == [1]



def size_pretty(b):
    if abs(b) >= 1024 * 1024 * 1024:
        # GB
        return "{:.1f}GiB".format(b / (1024 * 1024 * 1024))
    elif abs(b) >= 1024 * 1024:
        # MB
        return "{:.1f}MiB".format(b / (1024 * 1024))
    elif abs(b) >= 1024:
        # KB
        return "{:.1f}KiB".format(b / (1024))
    else:
        # B
        return "{}B".format(b)


def indexsizes(cursor):
    """Return the sizes of all the indexes."""
    sql = """SELECT                       
          current_database(), schemaname, tablename, reltuples::bigint, relpages::bigint, otta,
          ROUND(CASE WHEN otta=0 THEN 0.0 ELSE sml.relpages/otta::numeric END,1) AS tbloat,
          CASE WHEN relpages < otta THEN 0 ELSE bs*(sml.relpages-otta)::bigint END AS wastedbytes,
          iname, ituples::bigint, ipages::bigint, iotta,
          ROUND(CASE WHEN iotta=0 OR ipages=0 THEN 0.0 ELSE ipages/iotta::numeric END,1) AS ibloat,
          CASE WHEN ipages < iotta THEN 0 ELSE bs*(ipages-iotta) END AS wastedibytes,
          indisprimary,
          indexdef
        FROM (
          SELECT
            rs.schemaname, rs.tablename, cc.reltuples, cc.relpages, bs, indisprimary, indexdef,
            CEIL((cc.reltuples*((datahdr+ma-
              (CASE WHEN datahdr%ma=0 THEN ma ELSE datahdr%ma END))+nullhdr2+4))/(bs-20::float)) AS otta,
            COALESCE(c2.relname,'?') AS iname, COALESCE(c2.reltuples,0) AS ituples, COALESCE(c2.relpages,0) AS ipages,
            COALESCE(CEIL((c2.reltuples*(datahdr-12))/(bs-20::float)),0) AS iotta -- very rough approximation, assumes all cols
          FROM (
            SELECT
              ma,bs,schemaname,tablename,
              (datawidth+(hdr+ma-(case when hdr%ma=0 THEN ma ELSE hdr%ma END)))::numeric AS datahdr,
              (maxfracsum*(nullhdr+ma-(case when nullhdr%ma=0 THEN ma ELSE nullhdr%ma END))) AS nullhdr2
            FROM (
              SELECT
                schemaname, tablename, hdr, ma, bs,
                SUM((1-null_frac)*avg_width) AS datawidth,
                MAX(null_frac) AS maxfracsum,
                hdr+(
                  SELECT 1+count(*)/8
                  FROM pg_stats s2
                  WHERE null_frac<>0 AND s2.schemaname = s.schemaname AND s2.tablename = s.tablename
                ) AS nullhdr
              FROM pg_stats s, (
                SELECT
                  (SELECT current_setting('block_size')::numeric) AS bs,
                  CASE WHEN substring(v,12,3) IN ('8.0','8.1','8.2') THEN 27 ELSE 23 END AS hdr,
                  CASE WHEN v ~ 'mingw32' THEN 8 ELSE 4 END AS ma
                FROM (SELECT version() AS v) AS foo
              ) AS constants
              GROUP BY 1,2,3,4,5
            ) AS foo
          ) AS rs
          JOIN pg_class cc ON cc.relname = rs.tablename
          JOIN pg_namespace nn ON cc.relnamespace = nn.oid AND nn.nspname = rs.schemaname AND nn.nspname <> 'information_schema'
          LEFT JOIN pg_index i ON indrelid = cc.oid
          LEFT JOIN pg_class c2 ON c2.oid = i.indexrelid
          LEFT JOIN pg_indexes on pg_indexes.indexname = c2.relname
        ) AS sml
        ORDER BY wastedbytes DESC;"""

    cursor.execute(sql)

    #raw_results = cursor.fetchall()

    objs = {}
    for row in cursor.fetchall():
        if row['indexdef']:
            objs["{}.{}".format(row['schemaname'], row['iname'])] = {
                'schemaname': row['schemaname'],
                'iname': row['iname'],
                'name': row['iname'],
                'size': row['ipages'] * 8192,
                'type': 'index',
                'table': row['tablename'],
                'primary': row['indisprimary'],
                'def': row['indexdef'],
                'wasted': row['wastedibytes'],
                'indexdef': make_indexdef_concurrent(row['indexdef']),
            }

    objs = objs.values()
    objs.sort(key=lambda t: t['wasted'])

    # TODO should probably do this in the SQL query above.
    objs = [o for o in objs if o['schemaname'] == 'public' and o['wasted'] > 0]

    return objs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--database', type=str, required=True, help="PostgreSQL database name")
    parser.add_argument('-U', '--user', type=str, required=False, help="PostgreSQL database user")
    parser.add_argument('-n', '--dry-run', action="store_true", help="Dry run")
    args = parser.parse_args()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    logger.addHandler(handler)

    connect_args = {}
    if args.database is not None:
        connect_args['database'] = args.database
    if args.user is not None:
        connect_args['user'] = args.user

    conn = psycopg2.connect(**connect_args)

    # Need this transaction isolation level for CREATE INDEX CONCURRENTLY
    # cf. http://stackoverflow.com/questions/3413646/postgres-raises-a-active-sql-transaction-errcode-25001
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    objs = indexsizes(cursor)

    total_used = sum(Decimal(x['size']) for x in objs)
    total_wasted = sum(Decimal(x['wasted']) for x in objs)
    percent_wasted = "" if total_used == 0 else "{:.0%}".format(float(total_wasted)/float(total_used))
    logger.info("used:   {} ({:,}) wasted: {} ({:,}) {}".format(size_pretty(total_used), total_used, size_pretty(total_wasted), total_wasted, percent_wasted))

    total_savings = 0.0

    while True:

        for obj in objs:
            if obj['wasted'] == 0:
                logger.info("Skipping Index {name:>50} size {size:>15,} wasted {wasted:>15,}".format(**obj))
                continue
            if ' UNIQUE ' in obj['indexdef'].upper():
                # FIXME Better unique index detection
                # FIXME Don't skip unique indexes, instead figure out how to
                # recreate the unique contraint, like we do with PRIMARY KEYS
                logger.info("Skipping Index {} size {} ({:,}) wasted {} ({:,}) because it has a unique constrainst".format(obj['name'], size_pretty(obj['size']), obj['size'], size_pretty(obj['wasted']), obj['wasted']))
                continue

            oldsize = index_size(cursor, obj['name'])
            logger.info("Reindexing {} size {} ({:,}) wasted {} ({:,}) {:.0%}".format(obj['name'], size_pretty(obj['size']), obj['size'], size_pretty(obj['wasted']), obj['wasted'], float(obj['wasted']) / obj['size']))

            if not args.dry_run:
                old_index_name = "{t}_old".format(t=obj['name'])

                if does_index_exist(cursor, old_index_name):
                    logger.info("The index {old} already exists. This can happen when a previous run of this has been interrupted. You can delete this old index with:  DROP INDEX {old};  Processing will continue with the rest of the indexes".format(old=old_index_name))
                    continue

                cursor.execute("ALTER INDEX {t} RENAME TO {old};".format(t=obj['name'], old=old_index_name))
                cursor.execute(obj['indexdef'])
                cursor.execute("ANALYSE {t};".format(t=obj['name']))

                if obj['primary']:
                    cursor.execute("ALTER TABLE {table} DROP CONSTRAINT {t}_old, ADD CONSTRAINT {t} PRIMARY KEY USING INDEX {t};".format(t=obj['name'], table=obj['table']))

                cursor.execute("DROP INDEX {old};".format(old=old_index_name))

                newsize = index_size(cursor, obj['name'])
                delta_size = newsize - oldsize
                total_savings += delta_size
                logger.info("Saved {} ({:,}) {:.0%}".format(size_pretty(delta_size), delta_size, delta_size/oldsize))

        # TODO in future look at disk space and keep going
        break

    logger.info("Finish. Saved {} ({:,}) in total".format(size_pretty(total_savings), total_savings))


if __name__ == '__main__':
    main()
