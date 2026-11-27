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
