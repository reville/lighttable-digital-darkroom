#!/bin/sh
set -eu

collection_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$collection_root/../.." && pwd)
cd "$collection_root"

raw_count=$(find files -maxdepth 1 -type f | wc -l | tr -d ' ')
manifest_count=$(awk 'NR > 1 { count++ } END { print count + 0 }' manifest.tsv)
digest_count=$(wc -l < SHA256SUMS | tr -d ' ')

if [ "$raw_count" -ne 35 ] || [ "$manifest_count" -ne 35 ] || [ "$digest_count" -ne 35 ]; then
  echo "Expected 35 RAWs, 35 manifest rows, and 35 digests; found $raw_count, $manifest_count, and $digest_count." >&2
  exit 1
fi

shasum -a 256 -c SHA256SUMS

for application_bundle in \
  "$repository_root/build/LightTable.app" \
  "$repository_root/dist/LightTable.app"
do
  [ -d "$application_bundle" ] || continue
  for sample_file in files/*
  do
    sample_name=${sample_file##*/}
    if find "$application_bundle" -type f -name "$sample_name" -print -quit | grep -q .; then
      echo "Demo RAW leaked into build: $application_bundle/$sample_name" >&2
      exit 1
    fi
  done
  echo "Build excludes collection: $application_bundle"
done

echo "CC0/public-domain RAW collection verified."
