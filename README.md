# PhotoRoom

Bring the look of your Apple Photos edits into **Lightroom Classic**, while
keeping your originals editable. PhotoRoom compares your edited photos with
Lightroom renders and adjusts the Lightroom settings to get a close match.

It matches temperature, tint, exposure, contrast, highlights, shadows, whites,
blacks, crop, and rotation. Other settings stay unchanged. Matches are approximate;
review them before moving on from Apple Photos.

<img width="2336" height="1045" alt="photoroom2" src="https://github.com/user-attachments/assets/9321a1af-2b0e-434a-a950-a8fa62930697" />

## 1. Export from Apple Photos

Create two folders, `orig` and `edit`. Start with a few photos to try the workflow.

Select the same photos for both exports:

- **Originals:** choose **File > Export > Export Unmodified Original** and save
  to `orig`.
- **Edited versions:** choose **File > Export > Export Photos**, select **TIFF**,
  enable **16-bit**, choose **Full Size** and an RGB color profile such as
  **sRGB**, and save to `edit`.

Use the original filenames for both exports. Names must match apart from the
extension; if you use subfolders, their paths must match too:

```text
orig/IMG_001.ARW
edit/IMG_001.tiff
```

See [Apple’s export instructions](https://support.apple.com/guide/photos/pht6e157c5f/mac)
for help. PhotoRoom matches photo edits; it does not migrate albums or videos.

## 2. Set up Lightroom

You’ll need **macOS**, **Lightroom Classic 12+**, and **Python 3.10+**.
The launcher sets up its Python dependencies automatically.
[Homebrew](https://brew.sh) is needed only if LittleCMS, the color-processing
library, is missing.

1. Import the photos in `orig` into Lightroom using **Add**, so they stay in
   that folder.
2. Open **File > Plug-in Manager**, click **Add**, and select
   **`PhotoRoom.lrplugin`** inside the PhotoRoom project folder.
3. Choose **Library > Plug-in Extras > PhotoRoom: run matching bridge**.

The plug-in connects automatically and stays ready for later runs. Keep Lightroom
open and avoid editing photos while matching is running.

## 3. Run PhotoRoom

Open Terminal and go to your PhotoRoom project folder. Replace the example
paths below with your own folder paths. No separate installation commands or
environment activation are needed.

```bash
cd "/path/to/PhotoRoom"
```

**First, check one photo without making changes.** This previews the filename
pairing; it does not evaluate the match or require Lightroom.

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --dry-run --limit 1
```

**Next, match one photo.** Start the Lightroom bridge if it is not already
running, then run:

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit" --limit 1
```

Review that photo in Lightroom and its comparison preview before continuing.

**Then, match the full batch:**

```bash
python3 photoroom.py --orig "/path/to/orig" --edit "/path/to/edit"
```

PhotoRoom skips completed photos, including the one you just tested, then shows
matching progress and estimated time remaining.

## 4. Review the results

**Your matched edits are already in Lightroom’s catalog.** You do not need to
use “Read Metadata from File.” PhotoRoom does not write to your originals or
require Lightroom’s “Include Develop settings in metadata” checkbox.

Beside each edited TIFF, PhotoRoom saves:

- **`.match-preview.jpg`** — a comparison of the starting image, Apple Photos
  edit, and best match.
- **`.xmp`** — a record of the matched controls that also marks the photo as
  completed. This is not a full backup of your Lightroom edits.

The final summary lists completed photos and repeats any warnings or failures:

- **SAVED:** matching completed.
- **WARNING:** the closest result was saved, but needs review.
- **FAILED:** matching could not complete; check the accompanying message.

Lightroom can also write metadata itself. If you want originals to remain
untouched, turn off **Catalog Settings > Metadata > Automatically write changes
into XMP**.

## Run again

Run the same command whenever you’re ready. Completed photos are skipped.
**To redo one photo, delete its `.xmp` from `edit`**, then run again.
Its preview will be replaced automatically.

Add an option to the command when needed:

| Option | What it does |
| --- | --- |
| `--limit 1` | Try one photo that still needs matching |
| `--overwrite` | Redo all photos, including completed ones |
| `--virtual-copies` | Apply matches to virtual copies in Lightroom |
| `--prefer raw` | Use the RAW when a RAW and JPEG share the same name |

To stop the plug-in, choose **Library > Plug-in Extras > PhotoRoom: stop matching
bridge**. It exits after the current operation finishes and keeps completed matches.
If you move the PhotoRoom folder, stop the plug-in first and add it again from
its new location.

MIT licensed. See [LICENSE](LICENSE).
