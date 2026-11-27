# Usage

jwst-peculiar finds the sources in each field, cuts a colour stamp of every one big and bright enough, embeds the stamps with Zoobot and ranks them by how unlike every other object they are. Each step is a subcommand of `jwst-peculiar` and takes `--help`. [objects-plan.md](objects-plan.md) has how it was built, how well it does and the measurements behind each setting.

A YAML file says where a survey's mosaics are, with paths relative to the file. `init mosaics survey.yaml` writes one for a folder of mosaics, reading the field and filter out of names like MAST's `jw02736-o001_t001_nircam_clear-f090w_i2d.fits`, DJA's `egs-grizli-v7.2-f115w-clear_drc_sci.fits` or COSMOS-Web DR1's `mosaic_nircam_f277w_COSMOS-Web_60mas_A1_v1.0_i2d.fits`. It keeps the bands every field has, finds sources in the one nearest F277W, and colours the stamps with that, the one nearest F150W and the bluest. Gzipped mosaics work as they are but read slowly, and `unzip mosaics` expands them in place.

```yaml
name: cosmos-web
out: runs/cosmos-web          # everything the steps write goes here
pattern: mosaics/{field}_{band}_sci.fits
bands: [f115w, f150w, f277w, f444w]
detect: f277w                 # the band sources are found in
colour: [f277w, f150w, f115w] # red, green and blue of the stamps
stars: stars.csv              # optional, ra and dec of known stars
gaia: true                    # query Gaia DR3 for spikes, or a CSV, or false
device: auto
```

Without `fields`, every mosaic the pattern matches in the detect band is a field. Band names can be written either way (F115W or f115w), and `{BAND}` in a pattern fills in capitals for file names that have them.

`check` says what a config finds before any long step runs: the fields, any mosaic a field lacks, the memory `cut` needs, the disk the run needs (an upper bound), the device `embed` will use and whether the Zoobot weights are downloaded. A misspelled key or a pattern without `{band}` stops it with the reason. `run` does `check`, `cut`, `embed`, `rank` and, with F115W, F150W, F277W and F444W, `compact` in turn, and stops at the first that fails. Options after the config go to `cut`, like `run survey.yaml --redo`.

```
jwst-peculiar init mosaics survey.yaml  a config from the mosaics' file names
jwst-peculiar check survey.yaml         what the config finds, before a long run
jwst-peculiar run survey.yaml           check, cut, embed, rank and compact in turn
jwst-peculiar cut survey.yaml           detect, measure, flag and cut stamps
jwst-peculiar embed survey.yaml         Zoobot embeddings of every stamp
jwst-peculiar rank survey.yaml -n 500   the rarest systems, to rank.csv
jwst-peculiar rank survey.yaml --sample -n 200
jwst-peculiar rank survey.yaml --reference cosmos-web.yaml
jwst-peculiar like survey.yaml jw01727-o001_t001:5159
jwst-peculiar view survey.yaml --list sample.csv
jwst-peculiar label survey.yaml
jwst-peculiar compact survey.yaml -n 500
jwst-peculiar view survey.yaml --list compact.csv
jwst-peculiar view survey.yaml --export site/ --site-url https://example.org/demo/
```

## Cut

`cut` writes each field's objects to `fields/FIELD/objects.csv` with their aperture photometry in nJy and a `flag_*` column for every artefact test. A flagged object is left out of the ranking, though `flag_outshone` (a brighter neighbour in the stamp) only keeps it off the list. Each field is read into memory whole, about 31 bytes per pixel over all its bands. `cut` runs several fields at once, one for every four cores and no more than fit in half the machine's memory, and `--workers N` sets that number.

A mosaic too big for memory, like a whole JADES or UNCOVER field, can be cut in squares with `tile: 4000`, counted in detection-band pixels. Each square is its own field named `FIELD~ROW-COL`, read with a 12" margin so an object on a border comes out whole, and it keeps only the objects centred in it. `init` sets `tile` when a mosaic would need more than about 8 GB, and `check` suggests a size that fits. Tiled runs match the whole-mosaic run for all but 1% or so of objects, mostly faint ones, as the sky is fitted per tile.

## Rank, Like and Label

`rank` counts systems, so the stamps of one galaxy take one place between them. `rank --reference other.yaml` counts another run's objects as neighbours too, so a run of one or two fields can be ranked against what a bigger survey says is common, which works best when both use the same colour bands. `like` takes object ids or `RA,DEC` in degrees and lists the objects most like them.

`view` opens any of these lists in the viewer, where `1` to `4` mark an object interesting, an artefact, known or ordinary, saved to `verdicts.csv` in the run folder. Once there are 5 verdicts of each kind, `label` fits a classifier, and from then on `rank` multiplies rarity by its chance that an object is worth a look (`--image-only` turns that off). A known object counts as worth a look and stays off later lists. The list alone doesn't show the model what ordinary looks like, so label a `--sample` batch from places 101 to 2,000 now and then too. A new run can start from another run's `label_model.npz`, copied into its folder, as long as both use the same colour bands. `view --export` writes a list as a static site, and `--site-url` adds its social card and sitemap.

## Compact

`compact` ranks the sources too small for a stamp, which is where little red dots and brown dwarfs end up. When the config has F115W, F150W, F277W and F444W, `cut` writes each field's candidates to `fields/FIELD/compact.csv`: segments at S/N 10 or more with a semimajor axis of 0.12" or less or an area under 60 pixels, found in F277W or, where F277W has nothing, in F444W. Each gets these `flag_*` tests, and `compact` ranks the ones that pass every test on how rare their colours and concentrations are, averaged over three mixture fits, then writes a four-panel tile of each for `view`.

- `uncovered` and `edge`: uncovered sky in an aperture, or within 1" in any band.
- `line`: a thin straight line through it, which is a trail, a stripe or a spike.
- `one_band`: seen in only one band, or a one-band peak. A faint red candidate, brightest in F444W and centred at 2 sigma in F277W, counts as seen in both.
- `sharp`: more of its flux in 0.1" than a star has, which is a cosmic ray or hot pixel.
- `one_pair`: seen in only one of NIRCam's two exposure pairs.
- `dark_middle`: a band under S/N 1 between two at S/N 3.
- `listed_star`, `blue_star` and `gaia`: a known star, a point source with a star's colours too faint for Gaia, or a Gaia star's spikes or halo.
- `piece`: a deblended piece of a stamped object, inside its stamp.
- `row`: one of three like dots in an evenly spaced row, which is a source that moved between exposures.
- `pixel`: a lone one-band peak or a pixel far under the sky near it.

It finds known little red dots well, but expect about 30 of the top 100 to be artefacts or stars, and more on fields with short-wave striping.

## When Something Goes Wrong

Run `check` first, as it catches most of these before a long step does.

- `embed would run on cpu` on a machine with an NVIDIA GPU means pip installed the CPU build of PyTorch, which it does on Windows. Install the CUDA build from [pytorch.org](https://pytorch.org/get-started/locally/) into the same environment.
- `operator torchvision::nms does not exist` means torch and torchvision came from different builds. Install both with the same `--index-url`.
- `Couldn't query Gaia` means neither ESA's archive nor Heidelberg's copy answered after three more tries over about two minutes. Try later, point `gaia:` at a CSV of `ra`, `dec`, `phot_g_mean_mag` and `classprob_dsc_combmod_star`, or set `gaia: false` to skip the spike test.
- `Couldn't load hf_hub:mwalmsley/zoobot-encoder-convnext_nano` means the Zoobot weights couldn't be downloaded. They're fetched the first time `embed` runs and read from the cache after that.
- On a machine without internet, like most cluster nodes, set `gaia: false` or give a Gaia CSV, and copy a Hugging Face cache (`~/.cache/huggingface`) from a machine that ran `embed`, or set `HF_HOME` to a shared folder.
- `no compact list` from `check` means the config lacks one of F115W, F150W, F277W and F444W. The main list still runs.
- `the mosaics are in ..., so each band is turned into MJy/sr` is only a note. `the mosaics' units are ..., which can't be turned into MJy/sr` means a BUNIT like counts, and the colours are then only right if every band has the same unit and pixel size.
- `cut short or empty, so its download probably stopped early` means a mosaic is smaller than its header says. Download it again.
- A field that doesn't fit in memory needs `tile` set to the size `check` suggests, and `--workers 1` cuts one field at a time if something else needs the memory.
- A step stopped with Ctrl+C or a full disk can be run again, as outputs are written whole and finished fields are skipped.
- On a remote machine, run `view` with `--no-browser` and forward its port with `ssh -L 8765:127.0.0.1:8765 host`, as the viewer only listens on the machine it runs on. `Port 8765 is taken` just means another viewer is open, and this one picks a free port.
