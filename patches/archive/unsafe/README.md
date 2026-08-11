# Unsafe historical patches

Files in this directory are preserved only to explain old builds and reported
failures.  They must never be selected by current build scripts.

`v8-10.8-pr18.patch` is the patch merged in PR #18.  Besides bypassing the
source hash, it bypasses magic/version/flags/length/checksum checks, suppresses
deserializer synchronization failures, substitutes invalid objects, and turns
`HeapObjectShortPrint` into a recursive full printer.  Those changes can turn a
rejected cache into a corrupt object graph and match the crash shape in issue
#23.
