# Plan for the Object Pipeline

The tile searches in [search.md](search.md) and [minerva.md](minerva.md) came back empty, and their top tiles were mostly spikes, trails and seams. The pipeline here works per object instead, and its code joins the package once it passes the steps below.

This fixes how every change to the object pipeline gets judged, written on 2026-11-22 before any change to the prototype was made. The prototype detects sources on F277W, cuts one 224 px stamp per object scaled to its own size and brightness (F277W, F150W and F115W as red, green and blue), and embeds it with Zoobot's convnext_nano, averaged over 8 turns and mirrors. Rarity is the cosine distance to the fifth nearest object that shares no sky with it.

## The Split

COSMOS-Web has 148 observations and 185,811 prototype objects. A block of 21 neighbouring observations is the final test set, picked once with seed 0 as the 21 nearest one random observation more than 15 arcmin from the Infinity galaxy's at 16:53 on 2026-11-22. Those are o104, o106, o108, o110, o112, o114, o137 and o139 to o152. They hold 25,872 objects. Development objects are those in the other 127 observations that sit more than 1 arcsec from any test object, which leaves 159,788 of them in 141,672 systems (151 objects on shared edges belong to neither).

Nothing is tuned on the test block. It gets scored once, when the development set stops improving, and that result is reported whatever it says.

## The Suite

The known-answer suite scores a version of the pipeline on the development set. Ranks count systems rather than crops, so crops that share sky collapse into whichever comes first.

1. The Infinity galaxy (van Dokkum et al. 2025, 150.0591202 +2.2199215, in jw01727-o091_t061). It is ranked by rarity among o091's systems, and it is also searched for across all development systems from five degraded copies of its own stamp. Four are the prototype's (turned, shrunk to 0.75, noise of 12 in 8-bit, and all three at once). The fifth is turned, shrunk to 0.5 and given noise of 24, added because the prototype finds it at rank 1 from all four of the others.
2. Lenses. The list is the 440 COWLS candidates, the 19 AnomalyMatch COSMOS-Web candidates and the 7 extra COSMOS rows in AnomalyMatch's confirmed list, merged within 1 arcsec. Good lenses are COWLS M25, P18 and S07 or higher, plus grade or class A from AnomalyMatch. Seeded with each good lens in turn, the test counts how many other listed lenses fall in the nearest 50 and 100 systems, leaving out the seed's own crops.
3. Artefacts. The top 100 systems of the rarity list go on a contact sheet and on pages with each band in grey beside the colour stamp, and each one gets labelled by eye.
   - A, an artefact. A cosmic ray or hot pixel anywhere in the stamp (a one-band blob that saturates in the colour stamp and shows in only one grey band), a star or its spikes in the stamp, a satellite trail, or a stamp that reaches uncovered sky.
   - N, an ordinary galaxy whose stamp is dominated by a brighter neighbour.
   - O, an ordinary galaxy.
   - I, worth a second look, like a possible arc, a ring, a merger or an odd colour.

   The artefact rate is the share labelled A. A stamp that doesn't change between versions keeps its label, so only the objects new to a list get labelled.

## The Baseline

The prototype, measured on 2026-11-22.

| Test | Prototype |
|---|---|
| Infinity, rarity rank among o091's 1,366 systems | 3 |
| Infinity, rarity rank among all 141,672 development systems | 394 |
| Infinity from the four original seeds, rank among development systems | 1, 1, 1, 1 |
| Infinity from the hard seed, rank among development systems | 62 |
| Lenses, listed in the development set (good ones) | 352 (67) |
| Other listed lenses in the nearest 50, median and mean | 1, 1.51 |
| Other listed lenses in the nearest 100, median and mean | 2, 2.79 (chance 0.25) |
| Other good lenses in the nearest 100, mean | 0.85 |
| Logistic probe on the embeddings, listed lenses in its held-out top 100 | 15 |
| Top 100 rarity list, A, N, O, I | 28, 52, 12, 8 |

The 28 artefacts are 14 cosmic rays or hot pixels, 10 stars, 3 fields of diffraction spikes and 1 hole in the coverage. In nearly all of them the target is an ordinary galaxy and the artefact sits elsewhere in the stamp. The prototype's earlier figure of 35 for the Infinity galaxy came from ranking it against o091's objects alone, where this ranks it against the whole development set, which is how the tool is meant to be used.

## Bars Every Change Must Meet

A change is kept only if, on the development set, it passes all of these against the version currently adopted.

1. The Infinity galaxy is still in the list, unflagged, and ranks in the top 10 of o091's systems by rarity.
2. The four original seeds find it in the top 5 development systems and the hard seed in the top 124, twice the baseline.
3. The mean number of other listed lenses in the nearest 100 stays at 2.5 or more (the baseline less 10%), and at least 64 of the 67 good lenses stay in the list.
4. The artefact count in the top 100 is no higher than the adopted version's.

These allow for noise on a single object and a handful of lenses. No rule or threshold gets picked by looking at where the suite's own objects land, so every threshold comes from the physics or from objects outside the suite, and each change is judged once.

## Step 2, Artefact Rejection

Step 2 passes if, on top of the four bars, the top 100 has at most 10 artefacts, and the flags and merges together take out no more than 25% of development systems. A list can always be cleaned by dropping most of the sky, so that second limit is there to stop that.

### What Step 2 Does

Stamps and embeddings stay as they were, and an object is dropped when any of these holds.

- It shows at S/N 3 in fewer than two of the four bands.
- It is a catalogued star.
- Its stamp holds a one-band peak. That is a local peak in F115W, F150W or F277W whose excess over the median of a ring 0.25" out is at least 10 sigma, while in every other band the brightest pixel within 0.1" rises less than `max(4 sigma, excess / 20)` above its own ring. Testing against the ring rather than against empty sky is what catches a cosmic ray sitting on a galaxy, since the galaxy is there in all four bands. A real source of any colour shows in at least one other band at more than a twentieth of its contrast, as PSF peaks differ by at most about 8 between F150W and F444W.
- Its stamp holds a catalogued star at least a tenth as bright as the target.
- Its stamp crosses one of the six spikes or the halo of a Gaia DR3 star (G < 21, DSC star probability over 0.5). The spikes run at `PA_V3 + 60k` degrees, which the stars in these mosaics confirm, out to `4.7" x 10^(0.215 (18 - G))`, where the median spike falls to 1 sigma a pixel in F277W. The halo radius is `2.3" x 10^(0.073 (18 - G))`, measured the same way between the spikes. Both come from 1,921 star visits across the 148 observations.
- Its stamp reaches uncovered sky in a colour band.
- It is a deblended piece of a parent and sits inside the stamp of the parent's brightest surviving piece.

Every stamp test works in the stamp's own square, which lies on the F150W pixel grid and is turned against north.

### Step 2 Result

| Test | Prototype | Step 2 |
|---|---|---|
| Infinity, rarity rank among o091's systems | 3 of 1,366 | 3 of 1,206 |
| Infinity, rarity rank among development systems | 394 | 252 |
| Infinity from the four original seeds | 1, 1, 1, 1 | 1, 1, 1, 1 |
| Infinity from the hard seed | 62 | 37 |
| Other listed lenses in the nearest 100, mean | 2.79 | 3.03 |
| Good lenses still in the list | 67 | 64 |
| Logistic probe, listed lenses in its top 100 | 15 | 16 |
| Development systems removed | | 10.1% |
| Top 100 rarity list, A, N, O, I | 28, 52, 12, 8 | 4, 64, 18, 14 |

It passes, though with no room on the good lenses. The four artefacts left are three stars that are in neither the catalogue's star list nor Gaia's and one cosmic ray.

It took three tries. The first version tested one-band peaks against quiet sky and drew circles around Gaia stars brighter than G 16, and it left 21 artefacts. The pixels of three of those showed why: the cosmic rays sat on galaxies, so the other bands were never quiet. The second version brought in the contrast test and the spike geometry and got to 9. Six cosmic rays it missed then led to a bug, as the stamp tests had used a square aligned to north rather than the stamp's own. The fix made the third version. Looking at those suite objects' pixels bent the rule against tuning on the suite. No threshold was set from them, though, and every number above comes from the PSF, the star calibration or the geometry.

## Step 3, Making It Good

The goal for the finished pipeline, judged once on the test block, is a top 100 where at least 50 are worth a second look and at most 5 are artefacts. Step 3 starts by building a list of known odd objects from published catalogues (little red dots, jellyfish and ram-pressure galaxies, collisional rings, dusty edge-on disks, strong mergers and brown dwarfs, as well as lenses), split between the development and test sets by where they sit. Its recall bar goes into this file before any lever is tried.
