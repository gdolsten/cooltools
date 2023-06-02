import numpy as np
import pandas as pd
import bioframe as bf
import cooler


def generate_adjusted_chunks(clr, view, chunksize=1_000_000, orientation_col="strand"):
    """Generates chunks of pixels from the cooler and adjusts their bin IDs to follow the view"""
    view = view.copy()
    view = view.set_index("name")
    view_bin_ids = {
        region: clr.extent(view.loc[region, ["chrom", "start", "end"]])
        for region in view.index
    }
    view_max_bins = {region: ext[1] for region, ext in view_bin_ids.items()}
    orig_offsets = {
        region: clr.offset(view.loc[region, ["chrom", "start", "end"]])
        for region in view.index
    }
    bins_to_regions = {}
    for region, (start, end) in view_bin_ids.items():
        for bin in np.arange(start, end):
            bins_to_regions[bin] = region
    view["binlength"] = [i[1] - i[0] for i in view_bin_ids.values()]
    view["offset"] = np.append([0], np.cumsum(view["binlength"][:-1]))
    chunks = np.append(
        np.arange(0, clr.pixels().shape[0], chunksize), clr.pixels().shape[0]
    )
    chunks = list(zip(chunks[:-1], chunks[1:]))
    for i0, i1 in chunks:
        chunk = clr.pixels()[i0:i1]
        chunk["region1"] = chunk["bin1_id"].map(bins_to_regions)
        chunk["region2"] = chunk["bin2_id"].map(bins_to_regions)

        # Flipping where needed
        toflip1 = np.where(chunk["region1"].map(view[orientation_col] == "-"))[0]
        toflip2 = np.where(chunk["region2"].map(view[orientation_col] == "-"))[0]

        # add original offset because later it's subtracted from all bin IDs
        # then flip bin IDs by subtracting each ID from max ID of the region (binlength-1)
        chunk.loc[toflip1, "bin1_id"] = (
            chunk.loc[toflip1, "region1"].map(orig_offsets)
            + chunk.loc[toflip1, "region1"].map(view_max_bins)
            - 1
            - chunk.loc[toflip1, "bin1_id"]
        )
        chunk.loc[toflip2, "bin2_id"] = (
            chunk.loc[toflip2, "region2"].map(orig_offsets)
            + chunk.loc[toflip2, "region2"].map(view_max_bins)
            - 1
            - chunk.loc[toflip2, "bin2_id"]
        )

        # Rearranging
        chunk["bin1_id"] = (
            chunk["bin1_id"]
            - chunk["region1"].map(orig_offsets)
            + chunk["region1"].map(view["offset"])
        )
        chunk["bin2_id"] = (
            chunk["bin2_id"]
            - chunk["region2"].map(orig_offsets)
            + chunk["region2"].map(view["offset"])
        )

        # Drop unneeded technical columns
        chunk = chunk.drop(columns=["region1", "region2"])

        # Ensure bin1_id<bin2_id
        chunk[["bin1_id", "bin2_id"]] = np.sort(
            chunk[["bin1_id", "bin2_id"]].astype(int)
        )
        if chunk.shape[0] > 0:
            yield chunk.reset_index(drop=True)


def _adjust_start_end(chromdf):
    chromdf["end"] = chromdf["length"].cumsum()
    chromdf["start"] = chromdf["end"] - chromdf["length"]
    return chromdf


def _flip_bins(regdf):
    regdf = regdf.iloc[::-1].reset_index(drop=True)
    l = regdf["end"] - regdf["start"]
    regdf["start"] = regdf["end"].max() - regdf["end"]
    regdf["end"] = regdf["start"] + l
    return regdf


def _reorder_bins(
    bins_old, view_df, new_chrom_col="new_chrom", orientation_col="strand"
):
    chromdict = dict(zip(view_df["name"].to_numpy(), view_df[new_chrom_col].to_numpy()))
    flipdict = dict(
        zip(view_df["name"].to_numpy(), (view_df[orientation_col] == "-").to_numpy())
    )
    bins_old = bf.assign_view(bins_old, view_df, drop_unassigned=False).dropna(
        subset=["view_region"]
    )
    bins_inverted = (
        bins_old.groupby("view_region")
        .apply(lambda x: _flip_bins(x) if flipdict[x.name] else x)
        .reset_index(drop=True)
    )
    bins_new = bf.sort_bedframe(
        bins_inverted,
        view_df=view_df,
        df_view_col="view_region",
    )
    bins_new["chrom"] = bins_new["view_region"].map(chromdict)
    bins_new["length"] = bins_new["end"] - bins_new["start"]
    bins_new = (
        bins_new.groupby("chrom")
        .apply(_adjust_start_end)
        .drop(columns=["length", "view_region"])
    )
    return bins_new


def reorder_cooler(
    clr,
    view_df,
    out_cooler,
    new_chrom_col="new_chrom",
    orientation_col="strand",
    chunksize=1_000_000,
):
    """Reorder cooler following a genomic view.

    Parameters
    ----------
    clr : cooler.Cooler
        Cooler object
    view_df : viewframe
        Viewframe with new order of genomic regions. Needs an additional column for the
        new chromosome name, its name can be specified in `new_chrom_col`.
    out_cooler : str
        File path to save the reordered data
    new_chrom_col : str, optional
        Column name in the view_df specifying new chromosome name for each region,
        by default 'new_chrom'
    """
    if not np.all(
        view_df.groupby(new_chrom_col).apply(lambda x: np.all(np.diff(x.index) == 1))
    ):
        raise ValueError("New chromosomes are not consecutive")
    bins_old = clr.bins()[:]
    # Creating new bin table
    bins_new = _reorder_bins(bins_old, view_df, new_chrom_col=new_chrom_col)
    cooler.create_cooler(
        out_cooler,
        bins_new,
        generate_adjusted_chunks(
            clr, view_df, chunksize=chunksize, orientation_col=orientation_col
        ),
    )