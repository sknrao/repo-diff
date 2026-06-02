# Diff Reports Summary Table

> Generated from [diff_reports](file:///c:/Users/sridh/Documents/GitHub/repo-diff/diff_reports) — **6 repositories** compared on **2026-05-27**

| # | Current Repo | Upstream Repo | Branch (Current → Upstream) | Lines Added | Description of Additions |
|---|---|---|---|---:|---|
| 1 | [ios-mcn-smo/netconf](https://github.com/ios-mcn-smo/netconf) | [opendaylight/netconf](https://github.com/opendaylight/netconf) | `v6.0.6-ios-mcn` → `v6.0.6` | **+534** | Custom NETCONF protocol patches on top of OpenDaylight v6.0.6. Additions include modified `.git` metadata for the fork branch and configuration changes. Minor net additions (+271 delta) focused on fork-specific customizations. |
| 2 | [ios-mcn-smo/sdnc-oam](https://github.com/ios-mcn-smo/sdnc-oam) | [onap/sdnc-oam](https://github.com/onap/sdnc-oam) | `oslo-ios-mcn` → `oslo` | **+295** | Added a `settings.xml` (+178 lines) for Maven repository configuration, a GitHub Actions CI/CD pipeline (`maven.yml`, +65 lines), Docker build customizations for SDNC (+14 lines), and patched NETCONF/RESTCONF JAR binaries (netconf-api, netconf-client, restconf-nb, etc. as binary additions). POM dependency modifications in `installation/sdnc/pom.xml` (+22 lines). |
| 3 | [ios-mcn-smo/oam](https://github.com/ios-mcn-smo/oam) | [o-ran-sc/oam](https://github.com/o-ran-sc/oam) | `iosmcnmaster` → `l-release` | **+8,834** | Major fork divergence from upstream O-RAN SC OAM. Additions include: Grafana dashboards (`telegraf-system-dashboard.json` +4,892 lines, `aiab5g-dashboard.json` +694 lines), YAML configurations (+1,661 lines for new deployment configs), JSON data files (+5,739 lines), shell scripts (+431 lines), Python scripts (+131 lines), XML configs (+204 lines), conf files (+147 lines), TLS certificates/keys (+75 lines), and Markdown docs (+18 lines). The upstream removed ~1.2M lines of archived topology data (large JSON/SVG files from poc-fest archives), explaining the massive -1,248,044 removal count. |
| 4 | [ios-mcn-smo/oam-k8s](https://github.com/ios-mcn-smo/oam-k8s) | [o-ran-sc/it-dep](https://github.com/o-ran-sc/it-dep) | `m-release` → `main` | **+768** | Fork of O-RAN SC IT deployment (it-dep) repo as a streamlined K8s-focused variant. Additions include: Helm post-renderer image rewriting documentation (`rewrite-images.md`, +230 lines), custom README/INSTALL docs (+81 lines), shell scripts (+134 lines), requirements files (+80 lines), YAML deployment overrides (+34 lines), Helm chart patches (+34 lines). The upstream's large removal count (-28,909) reflects the removal of legacy `ric-aux`, `ric-common`, portal, and AAF Helm charts that are not carried in this fork. |
| 5 | [ios-mcn-smo/ccsdk-features](https://github.com/ios-mcn-smo/ccsdk-features) | [onap/ccsdk-features](https://github.com/onap/ccsdk-features) | `2.0.1-ios-mcn` → `2.0.1` | **+3,994** | Added a complete **Day Zero Config ODLUX application** — a new UI module for SDNR (SDN-R). Includes: React/TypeScript components (`editNetworkElementDialog.tsx` +319, `networkElements.tsx` +273, `configView.tsx` +206, etc.), service layers (`dayZeroService.ts` +190), Redux state handlers, action creators, model definitions, webpack config (+202), Maven `pom.xml` (+109), `package.json` (+44). Also added YANG model extensions for the data-provider (+102 lines), Java backend changes for `ElasticSearchDataProvider`, `SqlDBDataProvider`, and `DataProviderServiceImpl` to support day-zero config storage, plus a GitHub Actions CI pipeline (`maven.yml` +41 lines) and `settings.xml` (+178 lines). |
| 6 | [o-ran-sc/nonrtric-plt-ranpm](https://github.com/o-ran-sc/nonrtric-plt-ranpm) | [o-ran-sc/nonrtric-plt-ranpm](https://github.com/o-ran-sc/nonrtric-plt-ranpm) | `l-release` → `j-release` | **+2,799** | Cross-release comparison (L vs J release) of the same repo. Additions include: a large PM XML test file (+1,478 lines), a Gerrit-to-GitHub merge workflow (`gerrit-novote-merge.yaml` +218 lines), new `TS28532FileReadyMessage.java` model (+213 lines) and `DefaultFileReadyMessage.java` (+154 lines) for 3GPP-compliant file-ready message parsing, Go enhancements to `xmltransform.go` (+70 lines) and tests (+67), `dataTypes.go` additions (+63 lines), `FileCollector.java` improvements (+46 lines), release notes updates across datafilecollector/pm-file-converter/docs (+78 lines), Helm chart statefulset security context patches, Dockerfile updates, and `install-nrt.sh` enhancements (+22 lines). |

---

### Totals

| Metric | Value |
|---|---:|
| **Total Lines Added** | **17,224** |
| **Total Lines Removed** | **1,277,888** |
| **Total Files Changed** | **1,745** |
| **Repos Compared** | **6** |

> [!NOTE]
> The "Lines Added" column counts only additions (lines prefixed with `+` in the diff). The very large removal counts in **oam** and **oam-k8s** are primarily due to upstream archival data (large JSON/SVG topology files) and legacy Helm charts (ric-aux, AAF, portal) that exist in the upstream but not in the fork.
