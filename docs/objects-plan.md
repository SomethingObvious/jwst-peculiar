# Plan for the Object Pipeline

The tile searches in [search.md](https://github.com/SomethingObvious/jwst-peculiar/blob/f5469e1/docs/search.md) and [minerva.md](https://github.com/SomethingObvious/jwst-peculiar/blob/f5469e1/docs/minerva.md) came back empty, and their top tiles were mostly spikes, trails and seams. The pipeline here works per object instead, and since step 6 it is the package's search. The tile search was taken out in 0.9.0, which also dropped `objects` from the commands, so `jwst-peculiar objects run` below is `jwst-peculiar run` now.

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

### The Known Odd Objects

The held-out list comes from published catalogues and is matched to the catalogue within 0.6".

| Class | Source | Listed | In COSMOS-Web | In the prototype catalogue | Development | Test |
|---|---|---|---|---|---|---|
| Little red dots | Akins et al. 2025 | 434 | 429 | 4 | 4 | 0 |
| Brown dwarfs | Chen et al. 2025 | 27 | 27 | 0 | 0 | 0 |
| Rings | Galaxy Zoo: Hubble, COSMOS | 260 | 97 | 88 | 79 | 9 |
| Mergers | Galaxy Zoo: Hubble, COSMOS | 1,715 | 596 | 527 | 451 | 76 |
| Dusty edge-on disks | Galaxy Zoo: Hubble, COSMOS | 114 | 41 | 36 | 30 | 6 |
| Arcs | Galaxy Zoo: Hubble, COSMOS | 24 | 11 | 7 | 5 | 2 |
| Disturbed | Galaxy Zoo: Hubble, COSMOS | 10 | 1 | 1 | 1 | 0 |
| Lenses | the suite's list | 449 | 444 | 427 | 352 | 75 |

A Galaxy Zoo: Hubble galaxy counts when at least half its volunteers called it odd, at least 10 answered the odd-feature question, and at least half of those picked the feature. A dusty edge-on disk also needs at least half voting edge-on and at least half for a dust lane. No COSMOS-Web catalogue of jellyfish or ram-pressure galaxies has been published, so that class is empty.

An odd object counts as found at K when a system ranked K or better holds an object whose stamp centre is within `max(1", half)` of it. Recall is the share found within the top 1% and the top 5% of systems, over every class but lenses (lenses already have their own test).

| Recall, all classes but lenses | Prototype | Step 2 |
|---|---|---|
| Kept in the list | 570 of 570 | 489 of 570 |
| Found in the top 1% | 0.291 | 0.239 |
| Found in the top 5% | 0.637 | 0.523 |

Step 2 cost about a seventh of the known odd objects. Most were mergers and rings with a large stamp that a hot pixel, a spike or a catalogued star happened to cross, and the whole object was dropped for it. The point-like classes barely reach the catalogue at all, since it keeps only resolved objects at S/N over 40.

### Step 3 Bars

Every lever is judged on the development set against the version adopted at the time. On top of the four bars above, it must keep the top 5% recall over all classes but lenses within 0.03 of the adopted version's, and no class with 20 or more development members may fall by more than 0.05 there. A lever is kept when it passes those and improves at least one of the top 1% recall, the artefact count or the number worth a second look in the top 100.

The goal for the finished pipeline, judged once on the test block, is this.

1. At least 50 of the top 100 worth a second look, and at most 5 artefacts.
2. Over all classes but lenses, at least 0.35 found in the top 1% of systems and at least 0.70 in the top 5%.
3. At least half of the listed little red dots and brown dwarfs that fall in development are in the catalogue, so that the point-like classes can be found at all. That part is judged on development, since the test block holds none of them.

### Step 3 Result

Each lever was judged once on development against the version adopted at the time, and only labels won.

| Lever | Infinity, o091 and development rank | Hard seed | Recall, top 1% and top 5% | Top 100, A N O I | Verdict |
|---|---|---|---|---|---|
| Step 2 | 3, 252 | 37 | 0.239, 0.523 | 4, 64, 18, 14 | the base |
| Rarity among size and brightness peers | 70 in o091 | | 0.023, 0.147 | | fails the first bar |
| Clean stamps, brighter segments refilled | 2, 80 | 34 | 0.286, 0.539 | 9, 13, 51, 27 | fails on artefacts |
| Clean stamps, touching segments kept | 2, 254 | 39 | 0.254, 0.560 | 7, 66, 14, 13 | fails on artefacts |
| Clean stamps, blob-centred objects dropped | 2, 80 | 34 | 0.286, 0.539 | 8, 13, 52, 27 | fails on artefacts |
| F444W, F277W, F150W stamps joined | 2, 357 | 7 | 0.198, 0.484 | | fails on recall |
| Zoobot convnext_small | 19, 1,292 | 2,256 | 0.216, 0.449 | | fails |
| Zoobot convnext_base | 9, 846 | 1 | 0.209, 0.449 | | fails on recall |
| Labels, training objects left out | 2, 132 | 37 | 0.232, 0.512 | 2, 8, 62, 28 | adopted |

Labels work by putting a logistic regression (C 0.1, classes balanced) on the nano embeddings, which learns artefact or neighbour-dominated against the rest from the eye labels, and the score becomes `rarity * (1 - p_bad)`. Its cross-validated AUC on the first 102 labels is only 0.72, but that is enough to take the neighbour-dominated stamps from 64 of the top 100 to 8. Its numbers above are for a ranking with its own training objects left out, labelled fresh. I knew which version I was labelling, though, so there is some room for bias in the 28.

Clean stamps came closest on the number worth a look and failed each time on the cutouts the masking made. A mix of cleaned and uncleaned stamps makes the uncleaned ones rare, so cleaning has to treat every stamp alike, and a refill that stops at a segment's edge leaves a staircase under a big galaxy's halo. The joined long-wave view and convnext_base are worse for rarity but better for search by example (the hard seed comes back 7th and 1st), so they are worth keeping for `like`. The edge rule had never fired before, as these mosaics mark uncovered sky with 0 rather than NaN, and the fix goes forward.

The test block was then run once with the adopted pipeline, with the classifier trained on all 202 development labels and rarity computed among the block's 21,062 systems.

| Goal | Needed | Test block |
|---|---|---|
| Worth a second look in the top 100 | at least 50 | 27 |
| Artefacts in the top 100 | at most 5 | 5 |
| Recall over all classes but lenses, top 1% | at least 0.35 | 0.280 |
| Recall over all classes but lenses, top 5% | at least 0.70 | 0.602 |
| Little red dots and brown dwarfs in the catalogue | at least half | 4 of 434 and 0 of 27 |

It meets the artefact goal and misses the rest. The other 68 are 61 ordinary galaxies and 7 dominated by a neighbour. The artefacts are two low-coverage stamps, two stars and one masked hole. Mergers are found at 0.250 and 0.566, rings at 0.667 and 0.889, and lenses at 0.160 and 0.400. Image rarity alone mostly finds big, bright, well-resolved galaxies, which are rare in a field of small faint ones without being odd, and the catalogue's S/N 40 and area 60 cuts keep almost every point-like object out before ranking starts.

## The Second Test Block

The first test block has had its one run, so a second block of 21 observations was set aside on 2026-11-23 for judging the finished pipeline once more. It is the 21 development observations nearest one picked with seed 1 among those over 15 arcmin from the Infinity galaxy's and from every first-block observation. Those are o021, o023 to o038, o067, o069, o161 and o165, holding 27,753 objects. From here on development leaves out both blocks and anything within 1" of them, which leaves 131,873 objects, and no label on a second-block object trains anything.

On the smaller development set the adopted pipeline (step 2's rules, the nano embeddings and the label weighting) puts the Infinity galaxy 3rd in o091 and 261st overall, finds 0.224 of the known odd objects in the top 1% and 0.507 in the top 5%, and has A 1, N 34, O 46, I 19 in its top 100.

## Step 4, Catalogue Physics

Step 4 measures four-band photometry for every F277W segment, the same detection as the prototype, and adds two things. The first is a physics score for the objects already ranked, from how rare an object's colours are, and a combined score from it and the image score. The image-only score stays available. The second is a compact branch, the segments the prototype's size and S/N cuts throw out, ranked on physics alone, which is where little red dots and brown dwarfs live.

### Step 4 Bars

The combined score is judged on development against the adopted pipeline with the step 3 bars, so it has to pass the four bars, keep the top 5% recall within 0.03 with no class of 20 or more falling by over 0.05, and improve the top 1% recall, the artefact count or the number worth a look.

The compact branch is judged on development by these.

1. At least half of the development little red dots and brown dwarfs are in its catalogue.
2. At least half of those in its catalogue are found in its top 5%.
3. At most 10 artefacts in its top 100, as compact sources are where hot pixels and snowballs end up.

The goal for the finished pipeline on the second block is the step 3 goal, with the little red dot and brown dwarf part judged on the compact branch.

### Step 4 Result

Before the photometry finished, a three-class classifier (bad, ordinary and worth a look) on the nano embeddings was trained on 379 development labels, 200 of them drawn at random from places 101 to 2,000, and scored as `rarity * p(worth a look)`. It was judged on development against the adopted pipeline with the step 3 bars and adopted.

| Version | Infinity, o091 and development rank | Recall, top 1% and top 5% | Top 100, A N O I | Verdict |
|---|---|---|---|---|
| Two-class labels, the base | 3, 261 | 0.224, 0.507 | 1, 34, 46, 19 | the base |
| Three-class labels | 1, 90 | 0.269, 0.558 | 1, 4, 15, 80 | adopted |
| Three-class labels and physics, combined | 1, 263 | 0.213, 0.449 | | fails on recall |

The top 100s are ranked with every labelled object left out and labelled fresh. The combined score adds colour rarity by Fisher's method and loses mergers (0.579 to 0.441 at the top 5%) and rings (0.500 to 0.339), though it lifts dusty edge-on disks from 0.080 to 0.440 at the top 1%. The image score stays as it is.

| Compact branch bar | Needed | Development |
|---|---|---|
| Little red dots and brown dwarfs in its catalogue | at least half | 164 of 328 |
| Of those, found in its top 5% | at least half | 161 of 164 |
| Artefacts in its top 100 | at most 10 | 23 in the first 40 |

The compact branch fails on artefacts. Ten are clumps of strongly negative pixels in one band, which aren't NaN or 0 and so pass the coverage test, ten are cosmic rays or hot pixels the 0.1" aperture test misses when a faint galaxy lies under them, and three look like moving objects. The two proposed changes are to count pixels more than 5 sigma below the local sky within the 0.25" aperture as bad, and to use step 2's pixel-level blob rule on compact candidates in place of the aperture test.

### Step 4, Second Round

The two proposed changes went in along with anything else that looked like it would help. These were all tried on development, with rules chosen while looking at development sheets, so the second block stays the only fair judge.

A three-class model refit on 764 development labels (200 more drawn at random from places 101 to 2,000) puts the Infinity galaxy 71st but drops rings at the top 5% from 0.500 to 0.371, so the adopted model stays.

The compact branch now also detects in F444W, keeping sources not already on an F277W segment of S/N 10 or more, which finds 143 of the 145 development little red dots and brown dwarfs that had no F277W segment. On top of step 2's rules it drops a candidate when

1. a pixel more than 5 sigma below the sky, or a lone peak by step 2's blob rule in any band, lies within 0.25" plus 1.5 of that band's pixels,
2. it is sharper than a star, its 0.1" to 0.25" flux ratio over the 99th percentile of 742 Gaia stars (0.88, 0.865, 0.70 and 0.61 from F115W to F444W) by 3 sigma in one band or combined,
3. it shows in only one of NIRCam's two exposure pairs (F115W with F277W, F150W with F444W),
4. a band between two detected ones is under S/N 1, or
5. it is a catalogued star brighter in F150W than F444W, or lies within 2" of a catalogued star 20 times brighter in F277W.

| Compact branch bar | Needed | Development |
|---|---|---|
| Little red dots and brown dwarfs in its catalogue | at least half | 236 of 328 |
| Of those, found in its top 5% | at least half | 233 of 236 |
| Artefacts in its top 100 | at most 10 | 32 |

It still fails on artefacts: 5 stars, 9 faint noise peaks near bright sources, 11 stripes, trails, ghosts and wisps, and 6 cosmic rays or hot pixels. Its top 100 has 58 worth a look, nearly all red point sources, 18 of them in the published lists.

### Step 6 Result

The pipeline is now `jwst-peculiar objects cut|embed|rank|like|label|view|compact`, driven by one YAML config. On all 148 COSMOS-Web fields `cut` took 2 hours 10 minutes on the laptop's CPU and `embed` 27 minutes on its GPU. Its detection is the prototype's to the pixel (same labels, positions and fluxes), and it keeps 99.4% of the same objects as the research version. Most of the rest comes from two research bugs it doesn't have: in some fields research re-ran detection and looked parents up by label, which gave pieces the wrong parent for the merge rule, and its edge test flagged zero-valued pixels from an earlier copy of the mosaics that the current files don't have.

`compact` reproduces the research compact branch. On o001 the same 1,834 F277W candidates pass, 8 differ at an S/N 3 threshold because the blank-sky positions behind the noise factor are drawn differently (fluxes are identical), and 35 of 36 F444W sources match.

From here the package is the reference. On development, with `objects label` fitted on the same 378 labels the adopted model was chosen with, it scores as follows.

| Development | Research version | Package |
|---|---|---|
| Infinity, o091 and development rank | 1, 90 | 2, 108 |
| Seeds, four easy and the hard one | 1, 1, 1, 1, 35 | 1, 1, 1, 1, 35 |
| Lenses listed and good | 267, 53 | 265, 52 |
| Recall, top 1% and top 5% | 0.269, 0.558 | 0.269, 0.560 |
| Top 100 with training objects left out, A N O I | 1, 4, 15, 80 | 1, 8, 21, 70 |
| Compact, little red dots and brown dwarfs in its catalogue | 236 of 328 | 238 of 330 |
| Compact, of those in its top 5% | 233 | 235 |
| Compact, top 100 A O I | 32, 10, 58 | 30, 9, 61 |

70 of the package's top 100 carried labels from earlier lists and 30 were labelled fresh, and the fresh ones came out worth a look less often, so some of that gap is labelling rather than ranking.

## The Final Run, Fixed Before It Happens

This was written on 2026-11-23, before anything has run on the second block, and it doesn't change after.

1. What runs. The package's `objects` commands as committed with step 6, unchanged between the development numbers below and the run. The main list uses the label model `objects label` fits on the 378 development labels the adopted model was chosen with (the 200 drawn at random from places 101 to 2,000 and the top 100s labelled before step 4's second round). The second batch of 200 isn't used, as the model fitted with it failed on rings. The compact list uses the rules in step 4's second round.
2. How it runs. A config lists the block's 21 observations, so rarity is among the block's own systems as in the first block's run, and the compact mixture is fitted to the block's own candidates, as anyone running it on their own survey would. `objects rank` writes the top 100 systems and `objects compact` the top 100 compact candidates.
3. How it's labelled. Both top 100s go into one list, shuffled with seed 2 and with rank and score taken off, and I label all 200 from numbered contact sheets with the rubric used so far (worth a look, ordinary, artefact). The two kinds still look different, as a compact candidate gets a four-panel tile, so which list an object came from isn't hidden. A second labeller labels 50 of them, drawn with seed 3, in the viewer from a run folder of their own, without seeing mine, and the agreement goes in the report beside the result.
4. What it's judged on. The main list has the step 3 goal: at least 50 worth a look and at most 5 artefacts in its top 100, and over all classes but lenses at least 0.35 of the known odd objects in the top 1% of systems and 0.70 in the top 5%. The compact list has its three bars: at least half the block's listed little red dots and brown dwarfs in its catalogue, at least half of those in its top 5%, and at most 10 artefacts in its top 100. A known odd object counts as worth a look.
5. After it. The numbers are reported as they come out, nothing is tuned on the block, and it isn't run again.

### The Final Run

It ran once on 2026-11-23 as fixed above, with the label model's file hashed before the run (sha256 starting 4e80acaa). The block has 27,753 objects, 24,342 systems after the flags, and 61,911 compact candidates of which 42,046 pass the compact tests. The 200 were labelled blind before the key was opened.

| Goal | Needed | Second block |
|---|---|---|
| Worth a second look in the main top 100 | at least 50 | 57 (43 as labelled, plus 14 known odd objects labelled ordinary or neighbour) |
| Artefacts in the main top 100 | at most 5 | 0 |
| Recall over all classes but lenses, top 1% | at least 0.35 | 0.407 |
| Recall over all classes but lenses, top 5% | at least 0.70 | 0.593 |
| Listed little red dots and brown dwarfs in the compact catalogue | at least half | 0 of 2 |
| Artefacts in the compact top 100 | at most 10 | 48 |

The main list meets three of its four goals and misses the top 5% recall. Mergers are found at 0.441 and 0.624, rings at 0.294 and 0.588, and lenses only at 0.016 and 0.234. The other 43 in its top 100 are 34 ordinary galaxies and 9 dominated by a neighbour, with no artefacts at all, where the first block's run had 5.

The compact list fails. Its top 100 has 31 worth a look, nearly all red point sources, 21 ordinary and 48 artefacts: 19 are bands of striping across the short-wave images, which this block has far more of than development did, 21 are noise, 3 are stars, and the rest are edges, a hot pixel, a spike and a wisp. The block holds only 2 listed little red dots, and neither is in the compact catalogue, so its first two bars can't really be judged here. The development numbers (238 of 330 in the catalogue, 235 of them in the top 5%) are the better guide to recall, and the artefact rate here is the honest guide to how much cleaning its list needs.

There's no second labeller's check. The planned 50 weren't labelled, so the top 100 counts above rest on one labeller's judgement, while the recall figures come from published catalogues and don't depend on it.

## A Third Test on Other Surveys, Fixed Before It Happens

Both COSMOS-Web test blocks are used up, and the compact tests changed a lot on 2026-11-24, all of it measured on development fields. So this was written on 2026-11-24, before anything has run on these surveys, and it doesn't change after.

1. What runs. The package as of commit c25cd62 (version 0.8.0), unchanged between now and the runs. The main list uses the label model fitted on COSMOS-Web development verdicts, copied into each run folder, as someone with a survey of their own and no verdicts would. The compact list uses its committed rules.
2. Where. Two surveys the tool has never seen, both from the public DAWN JWST Archive (version 7.2 mosaics, F115W, F150W, F277W and F444W, short-wave bands at 0.02" and long-wave at 0.04"): UNCOVER around the cluster Abell 2744, and JADES in GOODS-South. Each runs whole with `objects run`, tiled as `init` and `check` say, so rarity is among each survey's own systems and the compact mixture is fitted to its own candidates.
3. How it's labelled. For each survey the main and compact top 100s go into one list, shuffled with seed 4 and with rank and score taken off, and I label all 200 from numbered contact sheets with the rubric used so far (worth a look, ordinary, artefact) before any published catalogue is matched to them.
4. What it's judged on, for each survey. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. A cluster field is full of lensed arcs, so worth a look may come easily in UNCOVER. The compact list: of the little red dots in Kocevski et al. (2025) inside the mosaic (46 in JADES and 23 in UNCOVER before coverage), at least 70% in the compact catalogue and at least 60% in its top 5%, and at most 40 artefacts in its top 100. Development gave 90%, 89% and 30, so these allow for a new survey being harder.
5. After it. The numbers are reported as they come out, nothing is tuned on these surveys, and they aren't run again.

### Third Test Result

The plan above didn't hold in two ways, and both are set out here before the numbers.

First, 0.8.0 couldn't run these surveys at all. Tiled configs crashed on tiles off a mosaic's edge, in five different ways, the last of which only showed once the others were fixed. None of those runs wrote a list. The fixes went in through d028531 and change nothing on a field with data, so a CEERS pointing gives 0.8.0's output byte for byte.

Second, the d028531 run finished, and the first contact sheet showed every main stamp nearly pure red. DJA's mosaics are in 10 nJy per pixel, and their short-wave pixels have a quarter of the long-wave pixels' area, so the package read the short-wave bands 4 times too faint. That turned the stamps red and put every colour 1.5 mag out, compact list included, and `check` had said the units didn't matter. 6ac8381 turns each band into MJy/sr with its own pixel area. Mosaics already in MJy/sr, COSMOS-Web, CEERS and SMACS 0723 among them, get a factor of exactly 1. All output and that sheet were deleted and both surveys run again from scratch. Before that run, 20 shuffled entries of the broken UNCOVER sheet had been seen, with no ranks and no labels given. Nothing of JADES had been seen. No threshold, rule or model changed, but the surveys were run again where the plan says they wouldn't be, so this is closer to a fresh test than the one fixed above.

The counted run used 6ac8381 with the COSMOS-Web label model (sha256 starting 4e80acaa) in each run folder. UNCOVER has 14,295 objects and 23,250 unflagged compact candidates, JADES 13,794 and 31,109. All 400 pooled entries were labelled blind before either key was opened.

| Goal | Needed | UNCOVER | JADES |
|---|---|---|---|
| Worth a look in the main top 100 | at least 50 | 53 | 45 |
| Artefacts in the main top 100 | at most 5 | 3 | 3 |
| Kocevski little red dots in the compact catalogue | at least 70% | 78% (18 of 23) | 79% (27 of 34) |
| The same in the compact top 5% | at least 60% | 70% (16 of 23) | 76% (26 of 34) |
| Artefacts in the compact top 100 | at most 40 | 29 | 42 |

UNCOVER meets every goal. JADES meets three and misses two, its main list by 5 and its compact artefacts by 2.

The main lists are mostly mergers, close pairs, clumpy and ringed galaxies and, in UNCOVER, lensed arcs around the cluster. Their artefacts are satellite trails, one star and one galaxy whose bands look out of register. Neighbour-dominated stamps were rare (3 and 4).

The compact lists find the little red dots well on both surveys, better than the goals allow for. Their artefacts are mostly one kind the tests don't catch: two or three dots in a short line, sometimes one colour and sometimes a different colour each, which look like a moving object caught in separate exposures. They make up 10 of UNCOVER's 29 and 19 of JADES's 42. The rest are striping, noise and stars, as on COSMOS-Web. A test on the catalogue alone caught 3 of the 29, as in the four measured bands each dot looks like any faint red source. Telling them apart needs where each band's light sits in the images, so that test waits for a survey it can be checked on.

One labeller again, with no second check. The recall figures come from a published catalogue and don't depend on the labels.

### After the Third Test

The moving dots got a test of their own, `flag_row`, worked out on the two surveys' labelled pools, so what follows are development numbers and not a test. Run again with it, UNCOVER's compact top 100 went from 29 artefacts to 25, with none of its moving dots left, and JADES's from 42 to 37, of which 15 are still moving dots and nearly all of those are pairs. Worth a look stayed at 28 in UNCOVER and went from 19 to 21 in JADES. Kocevski's little red dots stayed where they were, except one UNCOVER source that now sits a single place outside the top 5% (1,148 of 22,951). The entries new to each top 100 were labelled from sheets of their own, which means I knew they were new.

The other lever was the label model. Fitted on COSMOS-Web's verdicts and one survey's main labels, it gave the other survey's main top 100 52 worth a look in place of 44 (JADES) and 59 in place of 51 (UNCOVER). Unfortunately it also took COSMOS-Web's development recall of rings in the top 5% from 0.452 to 0.210, and over all classes but lenses from 0.542 to 0.482, much as the second batch of labels did before. So it isn't used, and the main list is as it was.

## A Fourth Test on GOODS-North, Fixed Before It Happens

This was written on 2026-11-27, before anything has run on GOODS-North, and it doesn't change after. Its mosaics have been downloaded and expanded and nothing else.

1. What runs. The package as committed with this section, unchanged between now and the run. The main list uses the COSMOS-Web label model (sha256 starting 4e80acaa) copied into the run folder, as in the third test. The compact list uses its committed rules, `flag_row` among them.
2. Where. GOODS-North from the DAWN JWST Archive (version 7.4 mosaics, F115W, F150W, F277W and F444W), a survey the tool hasn't seen. It runs whole with `objects run`, tiled as `init` and `check` say.
3. How it's labelled. The main and compact top 100s go into one list, shuffled with seed 8 and with rank and score taken off, and I label all 200 from numbered contact sheets with the rubric used so far before any published catalogue is matched to them.
4. What it's judged on. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 40 artefacts in its top 100, and of the little red dots from Matthee et al. (2024) in Perger et al.'s (2025) list that fall inside the mosaic, at least 70% in the compact catalogue and at least 60% in its top 5%. There are only 7 of those before coverage, as the big JADES census of both GOODS fields hasn't published its table yet, so that part says little either way. How many moving dots are left in the compact top 100 is reported but isn't a goal.
5. After it. The numbers are reported as they come out, nothing is tuned on GOODS-North, and it isn't run again.

### Fourth Test Result

It ran once on 2026-11-27 as fixed above, on bae06e5 with the COSMOS-Web label model (sha256 starting 4e80acaa), in 17 minutes. GOODS-North has 16,401 objects and 33,661 unflagged compact candidates. All 200 pooled entries were labelled blind before the key was opened.

| Goal | Needed | GOODS-North |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 51 |
| Artefacts in the main top 100 | at most 5 | 1 |
| Matthee little red dots in the compact catalogue | at least 70% | 50% (3 of 6) |
| The same in the compact top 5% | at least 60% | 50% (3 of 6) |
| Artefacts in the compact top 100 | at most 40 | 38 |

It meets three goals and misses the recall pair. Of the 7 little red dots, 6 count as inside the mosaic, and the 3 it finds are at places 30, 167 and 290. Two of the misses sit where a band has no coverage, so their candidates are flagged as uncovered and at an edge, which is what those tests are for, and the third was never a candidate, as nothing was detected within 0.9" of it. With only 6 to go on, this says little about recall either way, as the plan expected.

The main list is mostly mergers, close pairs, ringed and clumpy galaxies and tidal tails, with one stamp crossed by a trail. The compact list's artefacts are mostly noise peaks (14), spikes (6), stripes (6) and trails (5), and only 3 are moving dots, where JADES had 19 before the row test. One labeller again, with no second check.

## Measurements Behind the Settings

These were measured while building each setting, mostly on COSMOS-Web development fields, so none of them is a held-out result.

- `init` on a CEERS pointing from MAST wrote a working config first time, and that run's top 50 had 2 artefacts, a satellite trail and a diffraction spike.
- The disk `check` gives is an upper bound, as SMACS 0723 took 2.5 bytes per mosaic pixel and COSMOS-Web 1.5. SMACS 0723's 121 Mpx peaked at 3.7 GB in `cut`.
- Cutting 4 COSMOS-Web fields at once took them from 3 minutes 17 seconds to 68 seconds on a 16-thread laptop, with the same output.
- CEERS in 2000-pixel tiles matched the whole-mosaic run for 99.6% of objects and 49 of the top 50, and the SMACS 0723 cluster for 98.6%.
- Ranking 101 COSMOS-Web fields one at a time with the rest as `--reference` put 158 known odd objects in the fields' top 20s instead of 137, and left the top 5% about the same (234 rather than 236).
- The compact `edge` test left recall of known little red dots and brown dwarfs as it was and took the artefacts in the labelled top 100 from 27 to 22.
- `flag_line` hit all three trails and one cosmic ray among 193 labelled candidates, and none of the interesting ones.
- Reading pixels far under the sky only within 0.1" in F115W and F150W took known little red dots in the top 5% from 216 to 223 and brown dwarfs from 17 to all 18.
- Counting a centred 2 sigma F277W detection as seen, and a lone F444W peak within 0.1" as the candidate's own centre, brought back 66 and 31 known little red dots, for 277 of 312 in the top 5%. Of the 50 in the top 100, 41 were worth a look and 4 artefacts.
- `blue_star` took the top 100 from 35 artefacts or stars to 30 without touching a known little red dot or brown dwarf.
- `flag_row` caught all 10 moving-source rows in UNCOVER's labelled pool and 16 of 27 in JADES's, and none of the 57 known little red dots in either or the 171 known little red dots and brown dwarfs on COSMOS-Web. Most of the rest in JADES are two dots, which look just like a close pair.
- Two single mixture fits share about 84 of their top 100, and two averages of three share 92.
- On a CEERS pointing about 8 of the compact top 50 were artefacts, which the line and halo tests brought to 3 clear ones and about 4 doubtful. Of Kocevski et al.'s (2025) 7 little red dots there, 5 pass every test and rank 2, 4, 38, 55 and 56 of 2710. One more was taken for a piece of a neighbour, and the brightest, at F444W 24.0, was too big a segment to be a candidate.

## Other Compact Bands on UNCOVER, Fixed Before It Happens

This checks whether `settings:` works with bands the compact tests weren't tuned on. UNCOVER is cut again with the compact bands F150W, F200W, F356W and F444W in place of F115W, F150W, F277W and F444W, and with F277W still finding sources so the candidates are the same. `psf_sharpest` for F200W and F356W is the 99th percentile of 0.1" over 0.25" flux among segments at S/N 100 or more in that cut, which gives the four default bands to within 0.02 on UNCOVER's own run. It isn't held out, as UNCOVER helped tune the row test, so it only says whether other bands work about as well.

With the default bands, 18 of Kocevski et al.'s 23 little red dots inside UNCOVER are in the catalogue and 15 are in the top 5%. The goal is at least 16 in the catalogue and at least 13 in the top 5%.

### Other Compact Bands Result

It meets both goals. With F150W, F200W, F356W and F444W, 19 of the 23 little red dots are in the catalogue and 16 are in the top 5%, against 18 and 15 with the default bands.

| Band | `psf_sharpest` measured | Default |
|---|---|---|
| F150W | 0.866 | 0.865 |
| F200W | 0.824 | |
| F356W | 0.663 | |
| F444W | 0.621 | 0.61 |

The cut ran with guesses of 0.84 for F200W and 0.66 for F356W, and `flag_sharp` was worked out again with the measured values before ranking, which flags 79 candidates rather than 100. The 99th percentile of bright segments also gives F115W 0.885 and F277W 0.720 on UNCOVER, against defaults of 0.88 and 0.70 from COSMOS-Web's Gaia stars. Only 16 Gaia stars at G 19 to 21 fall on UNCOVER's coverage, too few to measure from. This only covers recall, as nobody labelled the new compact list.

## A Fifth Test on PRIMER-UDS, Fixed Before It Happens

This was written on 2026-11-27, before anything has run on PRIMER-UDS, and it doesn't change after.

1. What runs. Version 0.10.0 as committed with this section, with no `settings:` in the config, so every threshold is its default. The main list uses the COSMOS-Web label model (sha256 starting 4e80acaa) copied into the run folder, as in the third and fourth tests.
2. Where. PRIMER-UDS North and South from the DAWN JWST Archive (version 7.2 mosaics, F115W, F150W, F277W and F444W), which the tool hasn't seen. Each runs whole with `run`, tiled as `init` and `check` say.
3. How it's labelled. The main and compact top 100s of both halves together, ranked as one run, go into one list shuffled with seed 9 and with rank and score taken off, and I label all 200 from numbered contact sheets before any published catalogue is matched to them.
4. What it's judged on. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 40 artefacts in its top 100, and of Kocevski et al.'s 117 PRIMER-UDS little red dots that fall inside the mosaics, at least 70% in the compact catalogue and at least 60% in its top 5%.
5. After it. The numbers are reported as they come out, nothing is tuned on PRIMER-UDS, and it isn't run again.

### Fifth Test Result

It ran once on 2026-11-27 as fixed above, with the COSMOS-Web label model, in about 40 minutes. PRIMER-UDS has 39,026 objects and 60,003 unflagged compact candidates. All 200 pooled entries were labelled blind before the key was opened.

| Goal | Needed | PRIMER-UDS |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 64 |
| Artefacts in the main top 100 | at most 5 | 0 |
| Kocevski little red dots in the compact catalogue | at least 70% | 74% (86 of 117) |
| The same in the compact top 5% | at least 60% | 71% (83 of 117) |
| Artefacts in the compact top 100 | at most 40 | 62 |

It meets four goals and misses the compact artefacts by a lot. This is the first test with enough known little red dots to say much about recall, and 83 of the 117 land in the top 5% of 60,003 candidates.

The main list is mergers, close pairs, dusty edge-on discs, rings and one lensed arc, with nothing broken in it. The compact list's artefacts are noise peaks (22), short-wave stripes and spikes (21), stars and their halos (9), hot pixels (8) and pieces of bright galaxies (2). The hot pixels are a kind none of the earlier tests showed, a hard one-pixel square in F277W and F444W at the same place with nothing in the short-wave bands. Both long-wave filters use the same detector and PRIMER seems to have dithered them alike, so a hot pixel passes the one-band and one-pair tests as if it were two detections. Of the 25 worth a look, most are red point sources and a few are red fuzzy ones. One labeller again, with no second check.

## A Core Test for Long-Wave Dots

The fifth test's compact list had 8 hot pixels, hard one-pixel squares in F277W and F444W and nothing in the short-wave bands. That prompted this, but every cut here was set on JADES, UNCOVER and COSMOS-Web, and nothing was measured on PRIMER-UDS. JADES has the same kind of dot, and more often a moving source's two or three dots, which the row test misses when there aren't three in a line.

The sharp test couldn't catch them, as the 0.25" aperture is about four times noisier than the 0.1" one, so a dot at S/N 10 isn't 3 sigma sharper than a star by that ratio. `cut` now also measures flux in 0.05", and the sharp test flags a candidate at S/N 5 or more in both long bands whose 0.05" flux is over 60% or under 15% of its 0.1" flux in both. The low side catches a candidate centred between a moving source's dots.

| Sample | Lowest | Highest |
|---|---|---|
| 243 known little red dots and brown dwarfs on COSMOS-Web, at their candidates (0.063" pixels) | 0.22 in F277W | 0.50 in F277W, 0.37 in F444W |
| 57 known little red dots in JADES and UNCOVER | 0.32 in F277W, 0.30 in F444W | 0.46, 0.37 |
| Bright point sources on COSMOS-Web, JADES and UNCOVER | | 0.49 in F444W |
| 12 lone dots and squares in a JADES sample | 0.85 in F277W, 0.67 in F444W | |

On JADES it flags 200 more candidates and leaves 75 fewer unflagged. All 36 unflagged candidates with a hollow core (both under 0.2) were moving dots by eye, and 15 of the 19 entries that left the top 100 were dots or squares, while the other 4 were pushed out by the refit. Six moving dots still get in, as a point between dots is often under S/N 5 in 0.1". The top 100 has 3 fewer artefacts, and Kocevski recall is unchanged at 27 in the catalogue and 26 in the top 5%. On UNCOVER every candidate it hits was flagged already, and on COSMOS-Web's development top 100 it hits nothing. Re-cutting the development CEERS pointing changed no other column.

## A Sixth Test on CEERS, Fixed Before It Happens

This was written on 2026-11-27, before anything has run on the full CEERS mosaics, which have been downloaded and nothing else, and it doesn't change after.

1. What runs. The package as committed with this section, with no `settings:` in the config. The main list uses the COSMOS-Web label model (sha256 starting 4e80acaa) copied into the run folder.
2. Where. CEERS from the DAWN JWST Archive (version 7.4 mosaics, F115W, F150W, F277W and F444W), run whole with `run`, tiled as `init` and `check` say. One CEERS pointing (jw01345-o001_t021) was used in development, so anything on its F277W coverage is left out. The pool takes the first 100 of each list off it, and recall counts the little red dots off it, 57 of Kocevski et al.'s 64 before coverage.
3. How it's labelled. The two top 100s go into one list shuffled with seed 10 and with rank and score taken off, and I label all 200 from numbered contact sheets before any published catalogue is matched to them.
4. What it's judged on. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 40 artefacts in its top 100, and of those little red dots that fall inside the mosaic, at least 70% in the compact catalogue and at least 60% in its top 5%.
5. After it. The numbers are reported as they come out, nothing is tuned on CEERS, and it isn't run again.

### Sixth Test Result

It ran once on 2026-11-27 as fixed above, with the COSMOS-Web label model, in 25 minutes. CEERS has 18,288 objects and 30,386 unflagged compact candidates off the development pointing. All 200 pooled entries were labelled blind before the key was opened.

| Goal | Needed | CEERS |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 53 |
| Artefacts in the main top 100 | at most 5 | 4 |
| Kocevski little red dots in the compact catalogue | at least 70% | 81% (46 of 57) |
| The same in the compact top 5% | at least 60% | 79% (45 of 57) |
| Artefacts in the compact top 100 | at most 40 | 49 |

It meets four goals and misses the compact artefacts again, by less than on PRIMER-UDS. The compact list's artefacts here are mostly faint stars (20), with stripes (11), noise peaks (11), spikes (3) and pieces of bright galaxies (3), and only one moving source. The stars are blue point sources bright in F115W that the blue star test lets through. CEERS has few of the dots the core cuts are for, which flagged 6 candidates that no other test caught first. The main list's 4 artefacts are stamps crossed by a stripe. One labeller again, with no second check.

## Looser Star Colours

CEERS's compact list was mostly faint stars, and the blue star test wanted F150W a full magnitude over F277W and F277W half a magnitude over F444W. Only 31% of COSMOS-Web's listed stars above S/N 10 in F150W met both, as cool stars fall less steeply. The cuts are now 0.5 and 0.3 magnitudes, which 75% of those stars meet, and they were set on COSMOS-Web with JADES and UNCOVER to check, not on CEERS.

- COSMOS-Web: none of the 418 known little red dots, 27 brown dwarfs or 88 candidates labelled worth a look gets flagged. Of 40 newly flagged candidates no other test caught, 34 look like stars on their tiles and 6 are too faint or crowded to call.
- JADES and UNCOVER: about 500 more candidates each are flagged as stars. Recall stays at 27 and 18 Kocevski dots in the catalogue, and UNCOVER's top 5% goes from 15 to 16. The top 100s lose 7 and 6 entries, 4 and 5 of them stars now flagged.

Stripes were the next biggest group on CEERS, so the line test was tried with its side strips 0.5" to 1" out and with longer arms, on the labelled stripes of JADES and UNCOVER. No version caught more than half of either survey's stripes, and the ones that caught the most also flagged a candidate worth a look or a known little red dot, so the line test is unchanged.

## A Seventh Test on NGDEEP, Fixed Before It Happens

This was written on 2026-11-27, before anything has run on the NGDEEP mosaics, which have been downloaded and checked and nothing else, and it doesn't change after.

1. What runs. The package as committed with this section, with no `settings:` in the config. The main list uses the COSMOS-Web label model (sha256 starting 4e80acaa) copied into the run folder.
2. Where. NGDEEP from the DAWN JWST Archive (version 7.2 mosaics, F115W, F150W, F277W and F444W), run whole with `run`, tiled as `init` and `check` say. Anything on the F277W coverage of the JADES mosaic used in development is left out, both from the pool, which takes the first 100 of each list off it, and from recall, which counts Kocevski et al.'s 10 NGDEEP little red dots off it.
3. How it's labelled. The two top 100s go into one list shuffled with seed 11 and with rank and score taken off, and I label all 200 from numbered contact sheets before any published catalogue is matched to them.
4. What it's judged on. The same goals as the sixth test. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 40 artefacts in its top 100, and of those little red dots that fall inside the mosaic, at least 70% in the compact catalogue and at least 60% in its top 5%. With 10 dots or fewer, recall here says little on its own.
5. After it. The numbers are reported as they come out, nothing is tuned on NGDEEP, and it isn't run again. If the compact list misses its artefact goal again, that goal is recorded as unmet for surveys the package wasn't developed on, and the rounds of fixes stop there.

### Seventh Test Result

It ran once on 2026-11-27 as fixed above, with the COSMOS-Web label model, in 11 minutes. Off the JADES development mosaic, NGDEEP has 8,220 unflagged compact candidates. All 200 pooled entries were labelled blind before the key was opened.

| Goal | Needed | NGDEEP |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 47 |
| Artefacts in the main top 100 | at most 5 | 2 |
| Kocevski little red dots in the compact catalogue | at least 70% | 60% (6 of 10) |
| The same in the compact top 5% | at least 60% | 60% (6 of 10) |
| Artefacts in the compact top 100 | at most 40 | 43 |

It meets two goals and misses three. The compact list's artefacts are mostly short-wave stripes (20), with pieces of bright galaxies (8), noise peaks (7), faint stars (6) and spikes (2), so the looser star colours left 6 stars where CEERS had 20, and stripes took their place. The four little red dots it lost have no candidate within 0.6", the nearest 0.8" to 1.3" away, so they went missing before any flag was applied. The six it found are at places 2, 5, 6, 8, 11 and 72. The main list is 3 short of its goal for the first time since JADES. One labeller again, with no second check.

### Where the Rounds Stop

The compact list has now missed its artefact goal on JADES, PRIMER-UDS, CEERS and NGDEEP, and met it on UNCOVER and GOODS-North, so by the rule above that goal is recorded as unmet for surveys the package wasn't developed on and the rounds of fixes stop. Each round removed the kind of artefact the last survey had most of (moving dots, hot pixels, faint stars), and the next survey's top 100 filled up with another kind, stripes most often. On fresh surveys between 29 and 62 of its top 100 are artefacts. It still finds most known little red dots, though how many varies a lot by survey, from 3 of 6 on GOODS-North to 46 of 57 on CEERS.

## An Artefact Model for the Compact List

Paul asked for more rounds after the seventh test, so the stopping rule above is set aside at his request. The compact goal is now tighter as well, at most 20 artefacts in the top 100, and the rounds end when two fresh surveys in a row meet every goal.

Seven rounds of written tests each removed the kind of artefact the last survey had most of, and the next survey's top 100 filled up with another, so this round learns them from the labels instead. Zoobot's nano encoder is fine-tuned end to end with a one-unit head on every labelled compact tile from the blind pools and COSMOS-Web, plus 40 drawn from places 101 to 500 of each survey's list, with the known little red dots (Kocevski et al.'s and COSMOS-Web's) as tiles it must keep. That's 479 artefacts against 233 worth a look, 263 ordinary and 684 dots. There are two models, one on tiles as the sheets show them and one on tiles drawn by signal to noise, and a candidate's chance of being an artefact is the mean of both over its eight turns and mirror images. Frozen embeddings with a logistic regression caught only about 43% of the artefacts for the same loss, at any Zoobot size, and adding catalogue columns or the signal-to-noise tiles didn't change that much.

Judged leave one survey out, so each survey is scored by models that never saw it, the pair catches 410 of the 479 artefacts and drops 13 of 233 worth a look and 9 of the 684 known dots. The cut, 0.32, keeps 97% of the worth-a-look tiles and dots, with COSMOS-Web's 448 dots weighted 0.3 so they count about as much as one survey. It was picked from all seven surveys' held-out scores together, so it isn't quite blind to any one of them.

Each development survey was then ranked again with the models that left it out, and the new entries in its top 100 labelled the same way.

| Survey | Artefacts in its blind test | Artefacts with the models | Worth a look with them | 100th entry was at |
|---|---|---|---|---|
| UNCOVER | 29 | 8 | 51 | 345 |
| JADES | 42 | 8 | 56 | 325 |
| GOODS-North | 38 | 11 | 40 | 173 |
| PRIMER-UDS | 62 | 8 | 64 | 323 |
| CEERS | 49 | 13 | 47 | 287 |
| NGDEEP | 43 | 8 | 31 | 197 |

Every survey is under 20. These aren't blind numbers, though. I labelled the 316 new entries knowing each had passed the models, and the blind tests ran on earlier versions of the package. The eighth test below is the real check.

A looser size limit of 0.15" was tried as well, since some lost dots measure 0.12" to 0.14" across. It kept no more Kocevski dots on UNCOVER, JADES, PRIMER-UDS or CEERS and added between 3% and 14% more candidates, so the limit stays at 0.12".

## An Eighth Test on NEP-TDF, Fixed Before It Happens

This was written on 2026-11-28, before anything has run on the NEP-TDF mosaics, which have been downloaded and checked and nothing else, and it doesn't change after.

1. What runs. The package as committed with this section, with no `settings:` in the config. The main list uses the COSMOS-Web label model (sha256 starting 4e80acaa) copied into the run folder, and the compact list the artefact models trained on every development label (sha256 starting 48d9aefa), named by `artefacts:` in the config.
2. Where. The JWST NEP Time-Domain Field from the DAWN JWST Archive (version 7.0 mosaics, F115W, F150W, F277W and F444W), run whole with `run` in the two tiles `init` and `check` set. Nothing from it was used in development and no development survey overlaps it, so nothing is left out.
3. How it's labelled. The two top 100s go into one list shuffled with seed 12 and with rank, score and artefact chance taken off, and I label all 200 from numbered contact sheets with [labelling.md](labelling.md) before any catalogue is matched to them. A second labeller may label the same sheets from that page alone, and if so both sets of counts are reported.
4. What it's judged on. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 20 artefacts in its top 100. No published list of little red dots covers this field, so recall can't be judged here. It rests on the earlier blind tests and on the models dropping 9 of 684 known dots when judged leave one survey out.
5. After it. The numbers are reported as they come out, nothing is tuned on NEP-TDF, and it isn't run again. The rounds end once two fresh surveys in a row meet every goal. If this one passes, a ninth survey is fixed the same way before it runs. If it misses, the fix that follows is written up first and NEP-TDF doesn't count toward the two.

### Eighth Test Result

It ran once on 2026-11-28 as fixed above, with the COSMOS-Web label model and the artefact models (sha256 starting 48d9aefa), in 5 minutes. NEP-TDF has 2,233 objects and 2,969 unflagged compact candidates, and the artefact models put 994 of the top 2,000 compact candidates over the cut. All 200 pooled entries were labelled blind with [labelling.md](labelling.md) before the key was opened.

| Goal | Needed | NEP-TDF |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 31 |
| Artefacts in the main top 100 | at most 5 | 5 |
| Artefacts in the compact top 100 | at most 20 | 4 |

The compact list meets its goal by a wide margin, with 4 artefacts (a noise peak, a stripe, a faint star and a piece of a bright galaxy) and 50 worth a look, against 29 to 62 artefacts on every fresh survey before the models. The main list misses its worth-a-look goal badly and only just meets its artefact goal. This shallow field is full of bright, nearby spirals and red discs, which the rubric counts as ordinary however red they are, and earlier tests had no rubric written down, so 31 isn't quite comparable with their numbers. Three of the main list's artefacts are large, even red patches with no structure that I took for ghosts, though I'm not sure of them. So NEP-TDF doesn't count toward the two clean surveys, and the main list is what the next round has to fix. One labeller again, with no second check.

## A Pooled Label Model for the Main List

The main list's label model had only ever been fitted to COSMOS-Web's viewer verdicts, which is a big part of why NEP-TDF missed. So I fitted the same model to every survey's blind main labels plus those verdicts, and judged it leave one survey out, setting each survey's top 100 under a pooled model fitted without that survey against its top 100 under the COSMOS-Web model. This bar was written down before the comparison ran.

1. Summed over the seven blind surveys, the pooled model's top 100s hold at least 30 more worth a look.
2. It wins or ties on worth a look in at least five of the seven.
3. No survey's top 100 has more than 5 artefacts under it.
4. On COSMOS-Web, its top 5% recall of the known odd objects is no more than 0.05 below the COSMOS-Web model's, and its ring recall no more than 0.10 below. The COSMOS-Web model is scored on its own training survey there, which favours it.

The 238 entries in either top 100 with no label yet were labelled from shuffled sheets that didn't say which model put them there, with 10 already labelled entries a survey mixed in. Those 70 showed my labels had drifted a lot. Of 37 I'd called worth a look in the earlier tests, I called 24 ordinary this time, as the earlier tests had no rubric and this one had [labelling.md](labelling.md) open. An entry in both top 100s counts the same for both models, so all 434 entries in only one of them were then relabelled together in one shuffled sitting, and the bar was judged on those.

| Survey | In only one top 100 | Worth a look, pooled only | Worth a look, COSMOS-Web only | Artefacts in the pooled top 100 |
|---|---|---|---|---|
| UNCOVER | 80 | 19 | 10 | 0 |
| JADES | 62 | 18 | 2 | 4 |
| GOODS-North | 64 | 9 | 5 | 4 |
| PRIMER-UDS | 52 | 9 | 6 | 0 |
| CEERS | 58 | 9 | 6 | 2 |
| NGDEEP | 66 | 13 | 2 | 4 |
| NEP-TDF | 52 | 8 | 2 | 3 |
| All | 434 | 85 | 33 | |

On COSMOS-Web the top 5% holds 41.1% of the 1,149 known mergers, rings, lenses and arcs under the pooled model against 40.9%, and 26.8% of the 97 rings against 30.9%. So it passes all four, by 52 worth a look and on all seven surveys. Half of the 434 I had labelled an hour before, without knowing which model they came from, and it's one labeller again.

It doesn't fix the main list on its own, though. With the shared entries keeping their earlier labels, the pooled top 100s hold 33 to 62 worth a look, and four of the seven are still under 50, NEP-TDF lowest at 33. The final pooled model is fitted on all 1,301 main labels (107 artefacts, 744 ordinary, 450 worth a look), with sha256 starting 81c533fc. It's an ordinary `label_model.npz`, copied into a run folder like any other, and isn't in the repo as the hooks keep arrays out.

## A Ninth Test on MACS0647, Fixed Before It Happens

This was written on 2026-11-28, before anything has run on the six fresh mosaics, which have been downloaded and nothing else apart from the header check on MACS0647, and it doesn't change after.

1. What runs. The package as committed with this section, with no `settings:` in the config. The main list uses the pooled label model (sha256 starting 81c533fc) copied into the run folder, and the compact list the artefact models (sha256 starting 48d9aefa), named by `artefacts:` in the config.
2. Where. The six downloaded cluster fields from the DAWN JWST Archive go in the order `random.Random(9).sample` gives their sorted names, which is MACS0647, MRG0138, El Gordo, Abell S1063, Abell 1689 and then Sunrise. MACS0647 (version 7.0 mosaics, F115W, F150W, F277W and F444W) runs whole in the 12 tiles `init` and `check` set. No development survey overlaps it, so nothing is left out.
3. How it's labelled. The two top 100s go into one list shuffled with seed 13 and with rank, score and artefact chance taken off, and I label all 200 from numbered contact sheets with [labelling.md](labelling.md) before any catalogue is matched to them. A second labeller may label the same sheets from that page alone, and if so both sets of counts are reported.
4. What it's judged on. The main list: at least 50 worth a look and at most 5 artefacts in its top 100. The compact list: at most 20 artefacts in its top 100. No published list of little red dots covers this field, so recall isn't judged here. MACS0647 is a lensing cluster, which brings arcs and a crowd of cluster ellipticals, and nothing is changed for that.
5. After it. The numbers are reported as they come out, nothing is tuned on MACS0647, and it isn't run again. If both lists pass, MRG0138 is the tenth test with the same models and goals, and a pass there ends the rounds. If it misses, the fix that follows is written up first and the next survey in the order is used.

### Ninth Test Result

It ran once on 2026-11-28 as fixed above, with the pooled label model (sha256 starting 81c533fc) and the artefact models (sha256 starting 48d9aefa), in 6 minutes. Six of the 12 tiles fall off the mosaic, and MACS0647 has 2,157 objects, 1,103 of them unflagged, so the main top 100 is nearly a tenth of every object it could show. The artefact models put 1,180 of the top 1,868 compact candidates over the cut. All 200 pooled entries were labelled blind with [labelling.md](labelling.md) before the key was opened.

| Goal | Needed | MACS0647 |
|---|---|---|
| Worth a look in the main top 100 | at least 50 | 23 |
| Artefacts in the main top 100 | at most 5 | 3 |
| Artefacts in the compact top 100 | at most 20 | 17 |

The compact list meets its goal again, though by less than on NEP-TDF. 13 of its 17 artefacts are faint blue points with a sharp F115W dot that I took for stars, and it has 39 worth a look, mostly green points. The main list misses its worth-a-look goal by more than it did on NEP-TDF, with 23. Most of its top 100 is red discs, edge-on spirals and plain cluster members, and all 3 artefacts are pieces of bright galaxies. So MACS0647 doesn't count toward the two clean surveys either. The main list has now missed on the two smallest fields, and under the rubric a top 100 drawn from about 1,000 objects may not hold 50 worth a look at all, which the next round has to settle before it tunes anything. One labeller again, with no second check.
