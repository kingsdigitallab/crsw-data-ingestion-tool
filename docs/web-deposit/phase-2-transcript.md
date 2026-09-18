# Phase 2 checkpoint: curl transcript

Date: 18 September 2026. Stack: `docker compose -f deploy/compose.yaml up --build` on the developer laptop over the KCL VPN, host port 8090, service key = the developer's personal key as a stand-in for the staging-only key. Everything below is under `staging/_test/`.

Test suite at this commit: 404 pass under pytest (moto), 405 pass / 32 skipped under stdlib unittest, integration test against the real cluster passes. CLI `--dry-run` output unchanged from the pre-refactor baseline.

## Two-file deposit

```
$ curl -s -X POST localhost:8090/deposits -H "Content-Type: application/json" -d @meta.json
{"id":"dd85a7172677","prefix":"rs2/csac/green/0_raw/web-poc","staging_prefix":"staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc","dataset_uuid":"68f52788-cd31-4b18-a2d0-7f41aff5218c","warnings":[]}

$ curl -s -X PUT localhost:8090/deposits/dd85a7172677/files/one.csv --data-binary @fixture/one.csv
{"member":"one.csv","key":"rs2/csac/green/0_raw/web-poc/one.csv","staged_key":"staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/one.csv","bytes":8,"checksum_sha256":"492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470","format":"text/csv"}

$ curl -s -X PUT localhost:8090/deposits/dd85a7172677/files/sub/blob.bin --data-binary @blob.bin   # 9 MiB, multipart path
{"member":"sub/blob.bin","key":"rs2/csac/green/0_raw/web-poc/sub/blob.bin","staged_key":"staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/sub/blob.bin","bytes":9437184,"checksum_sha256":"b9118fa95a468f7ef6dfad6f6cc4866da607b56bfe841b5492338f9e1e2f844f","format":"application/octet-stream"}

$ curl -s -X PUT localhost:8090/deposits/dd85a7172677/files/sub/.DS_Store --data-binary ""
{"detail":"'sub/.DS_Store' looks like OS/editor noise and is excluded by default; re-send with ?include_noise=1 to deposit it anyway"}

$ curl -s -X POST localhost:8090/deposits/dd85a7172677/finalise
{"id":"dd85a7172677","record_key":"staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/dataset.web-poc.json","dataset_uuid":"68f52788-cd31-4b18-a2d0-7f41aff5218c","files":2,"warnings":[],"staging_prefix":"staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc"}

$ rclone lsjson --metadata --recursive k1078591:crsw/staging/_test/k1078591/dd85a7172677
_deposit.json                                                       1577  uuid=-.. sha=-.. sens=- dep=-
rs2/csac/green/0_raw/web-poc/dataset.web-poc.json                   1421  uuid=68f52788.. sha=8ab33bec.. sens=green dep=k1078591
rs2/csac/green/0_raw/web-poc/one.csv                                   8  uuid=68f52788.. sha=492d5ea4.. sens=green dep=k1078591
rs2/csac/green/0_raw/web-poc/sub/blob.bin                        9437184  uuid=68f52788.. sha=b9118fa9.. sens=green dep=k1078591
rs2                                                                   -1  uuid=-.. sha=-.. sens=- dep=-
rs2/csac                                                              -1  uuid=-.. sha=-.. sens=- dep=-
rs2/csac/green                                                        -1  uuid=-.. sha=-.. sens=- dep=-
rs2/csac/green/0_raw                                                  -1  uuid=-.. sha=-.. sens=- dep=-
rs2/csac/green/0_raw/web-poc                                          -1  uuid=-.. sha=-.. sens=- dep=-
rs2/csac/green/0_raw/web-poc/sub                                      -1  uuid=-.. sha=-.. sens=- dep=-

$ rclone cat k1078591:crsw/staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/dataset.web-poc.json
{
  "schema_version": "0.5",
  "dataset_uuid": "68f52788-cd31-4b18-a2d0-7f41aff5218c",
  "identifier": "rs2/csac/green/0_raw/web-poc",
  "strand": "rs2",
  "project": "csac",
  "dataset": "web-poc",
  "sensitivity": "green",
  "state": "0_raw",
  "domain": "quant",
  "version": "1-0",
  "abstract": "A two-file deposit made with curl against the containerised web deposit service, to show that keys, object labels and the dataset record written to the staging area match what the command-line tool produces. Safe to delete at any time; it lives under staging/_test and contains random bytes and a tiny CSV.",
  "subject": [
    "forced-labour",
    "archival"
  ],
  "vocabulary_version": "2026-07-23",
  "temporal": {
    "start": "2020",
    "end": "2021"
  },
  "source_type": "archive",
  "source_detail": "Curl transcript for the Phase 2 checkpoint",
  "license": "CC-BY-4.0",
  "steward": "Kevin Fahey",
  "depositors": [
    "k1078591"
  ],
  "created": "2026-09-18T10:42:52Z",
  "modified": "2026-09-18T10:42:52Z",
  "files": [
    {
      "path": "one.csv",
      "checksum_sha256": "492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470",
      "bytes": 8,
      "format": "text/csv"
    },
    {
      "path": "sub/blob.bin",
      "checksum_sha256": "b9118fa95a468f7ef6dfad6f6cc4866da607b56bfe841b5492338f9e1e2f844f",
      "bytes": 9437184,
      "format": "application/octet-stream"
    }
  ]
}
```

Notes:

- `_deposit.json` is the service's control object (no labels; the promoter skips it).
- Every deposited object and the record carry the four labels with the same `dataset-uuid`.
- The `.DS_Store` PUT was refused as noise; `?include_noise=1` would have accepted it.
- The record is what `deposit_logic.assemble_record` + `record_bytes` produce; the test suite asserts byte equality.

## Streaming check

A 1 GB random file PUT through nginx and the service (`curl -T`), sampling `docker stats` every second:

| | |
|---|---|
| Container memory idle | 68 MiB |
| Container memory peak during the 1 GB upload | 96 MiB |
| Upload time | 113 s (about 9.5 MB/s from this laptop over the VPN) |
| Result | HTTP 200, stored size 1073741824, checksum recorded |

Memory stays within one 8 MiB part plus overhead; nothing was written to disk (the container root filesystem is read-only and has no writable mounts).

## Left for the reviewer to inspect

The deposit above is still in staging at `staging/_test/k1078591/dd85a7172677/`. The memory-test deposits were removed with plain `delete_object` calls (delete markers).

## Finding: versioning and the developer key

The `crsw` bucket is versioned and the personal key used as the stand-in can add delete markers but cannot delete specific object versions (`DeleteObject` with a version id returns `AccessDenied`). `rclone purge` on a remote with `versions` enabled therefore fails; the service's own DELETE route and boto3 `delete_object` succeed. Two consequences for eResearch: the staging lifecycle rule must expire noncurrent versions as well as current objects, and the promoter's "delete from staging after copy" step must be defined in terms of delete markers unless its key is allowed to delete versions.

## Known gap carried to Phase 4

Every web deposit assembles its record as a first deposit (new `dataset_uuid`, `created` = now) because the staging-only key cannot read the destination prefix. The promoter must reconcile with any record already at the destination: keep the existing UUID and `created`, merge the manifest, append the depositor.
