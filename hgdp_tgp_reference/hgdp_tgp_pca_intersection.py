import hail as hl
import argparse
import re


def load_liftover():
    # liftover gbmi to GRCh38 (gnomAD + HGDP is much larger, so not as easy to liftover)
    rg37 = hl.get_reference('GRCh37')
    rg38 = hl.get_reference('GRCh38')
    rg38.add_liftover('gs://hail-common/references/grch38_to_grch37.over.chain.gz', rg37)  # 38 to 37
    rg37.add_liftover('gs://hail-common/references/grch37_to_grch38.over.chain.gz', rg38)  # 37 to 38
    return rg37, rg38


def ref_filtering(ref_mt, pass_mt, unrel, outliers, pass_unrel_mt, overwrite: bool = False):
    mt = hl.read_matrix_table(ref_mt)
    all_sample_filters = set(mt['sample_filters'])
    bad_sample_filters = {re.sub('fail_', '', x) for x in all_sample_filters if x.startswith('fail_')}
    mt_filt = mt.filter_cols(mt['sample_filters']['qc_metrics_filters'].difference(bad_sample_filters).length() == 0)
    mt_filt = mt_filt.checkpoint(pass_mt, overwrite=False, _read_if_exists=True)

    mt_unrel = hl.read_matrix_table(unrel)

    mt_filt = mt_filt.filter_rows(mt_filt.filters.length() == 0)  # gnomAD QC pass variants
    mt_filt = mt_filt.filter_cols(hl.is_defined(mt_unrel.cols()[mt_filt.s]))  # only unrelated

    # remove outliers
    pca_outliers = hl.import_table(outliers).key_by('s')
    mt_filt = mt_filt.filter_cols(hl.is_missing(pca_outliers[mt_filt.s]))

    mt_filt.write(pass_unrel_mt, overwrite)


def intersect_target_ref(ref_mt_filt, snp_list, grch37_or_grch38, intersect_out, overwrite: bool = False):
    mt = hl.read_matrix_table(ref_mt_filt)
    if grch37_or_grch38.lower() == 'grch38':
        snp_list = snp_list.key_by(locus=hl.locus(hl.str(snp_list.chr), hl.int(snp_list.pos), reference_genome='GRCh38'),
                                   alleles=[snp_list.ref, snp_list.alt])
        mt = mt.filter_rows(hl.is_defined(snp_list[mt.row_key]))

    elif grch37_or_grch38.lower() == 'grch37':
        snp_list = snp_list.key_by(locus=hl.locus(hl.str(snp_list.chr), hl.int(snp_list.pos), reference_genome='GRCh37'),
                                   alleles=[snp_list.ref, snp_list.alt])
        # liftover snp list to GRCh38, filter to SNPs in mt
        rg37, rg38 = load_liftover()

        snp_liftover = snp_list.annotate(new_locus=hl.liftover(snp_list.locus, 'GRCh38'))
        snp_liftover = snp_liftover.filter(hl.is_defined(snp_liftover.new_locus))
        snp_liftover = snp_liftover.key_by(locus=snp_liftover.new_locus, alleles=snp_liftover.alleles)
        mt = mt.filter_rows(hl.is_defined(snp_liftover[mt.row_key]))

    mt = mt.repartition(5000)
    mt = mt.checkpoint(intersect_out, overwrite = overwrite, _read_if_exists = not overwrite)


def ld_prune_filter(intersect_out, prune_out, overwrite: bool = False):
    mt = hl.read_matrix_table(intersect_out)
    print(mt.count())
    mt = hl.variant_qc(mt)
    mt_filt = mt.filter_rows((mt.variant_qc.AF[0] > 0.001) & (mt.variant_qc.AF[0] < 0.999))
    print(mt_filt.count())

    mt_intersect_prune = hl.ld_prune(mt_filt.GT, r2=0.8, bp_window_size=500000)
    mt_intersect_pruned = mt_filt.filter_rows(hl.is_defined(mt_intersect_prune[mt_filt.row_key]))
    mt_intersect_pruned.write(prune_out, overwrite)


def run_pca(prune_out: hl.MatrixTable, pca_prefix: str, overwrite: bool = False):
    """
    Run PCA on a dataset
    :param mt: dataset to run PCA on
    :param pca_prefix: directory and filename prefix for where to put PCA output
    :return:
    """

    mt = hl.read_matrix_table(prune_out)

    pca_evals, pca_scores, pca_loadings = hl.hwe_normalized_pca(mt.GT, k=20, compute_loadings=True)
    pca_mt = mt.annotate_rows(pca_af=hl.agg.mean(mt.GT.n_alt_alleles()) / 2)
    pca_loadings = pca_loadings.annotate(pca_af=pca_mt.rows()[pca_loadings.key].pca_af)

    pca_scores.write(pca_prefix + 'scores.ht', overwrite)
    pca_scores = hl.read_table(pca_prefix + 'scores.ht')
    pca_scores = pca_scores.transmute(**{f'PC{i}': pca_scores.scores[i - 1] for i in range(1, 21)})
    pca_scores.export(pca_prefix + 'scores.txt.bgz')  # individual-level PCs

    pca_loadings.export(pca_prefix + 'loadings.txt.bgz')

    pca_loadings.write(pca_prefix + 'loadings.ht', overwrite)  # PCA loadings

    #export loadings in plink format
    ht = hl.read_table(pca_prefix + 'loadings.ht')
    ht = ht.key_by()
    ht_loadings = ht.select(ID=hl.variant_str(ht.locus, ht.alleles), ALT=ht.alleles[1],
                            **{f"PC{i}": ht.loadings[i - 1] for i in range(1, 21)})
    ht_afreq = ht.select(**{"#ID": hl.variant_str(ht.locus, ht.alleles), "REF": ht.alleles[0], "ALT": ht.alleles[1],
                            "ALT1_FREQ": ht.pca_af})
    ht_loadings.export(pca_prefix + 'loadings.plink.tsv')
    ht_afreq.export(pca_prefix + 'loadings.plink.afreq')

def main(args):

    if args.run_ref_filtering:
        ref_filtering(args.ref_mt, args.pass_mt, args.unrel_mt, args.outliers, args.pass_unrel_mt, args.overwrite)

    if args.run_intersection:
        snp_list = hl.import_table(args.snp_list, impute=True)
        intersect_target_ref(args.pass_unrel_mt, snp_list, args.grch37_or_grch38, args.intersect_out, args.overwrite)

    if args.run_ld_prune_filter:
        ld_prune_filter(args.intersect_out, args.prune_out, args.overwrite)

    if args.run_pca:
        run_pca(args.prune_out, args.pca_prefix, args.overwrite)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ref_mt', default='gs://african-seq-data/hgdp_tgp/hgdp_tgp_dense_meta_filt.mt')
    parser.add_argument('--pass_mt', default='gs://african-seq-data/hgdp_tgp/temp_hgdp_tgp_pass_samples.mt')
    parser.add_argument('--unrel_mt', default='gs://african-seq-data/hgdp_tgp/unrel.mt')
    parser.add_argument('--outliers', default='gs://african-seq-data/hgdp_tgp/pca_outliers.txt')
    parser.add_argument('--pass_unrel_mt', default='gs://african-seq-data/hgdp_tgp/temp_hgdp_tgp_pass_unrel_samples.mt')
    parser.add_argument('--snp_list', help='white-space delimited table of chr, pos, a1, a2')
    parser.add_argument('--grch37_or_grch38', default='grch38')
    parser.add_argument('--intersect_out')
    parser.add_argument('--prune_out')
    parser.add_argument('--pca_prefix')

    parser.add_argument('--run_ref_filtering', action='store_true')
    parser.add_argument('--run_intersection', action='store_true')
    parser.add_argument('--run_ld_prune_filter', action='store_true')
    parser.add_argument('--run_pca', action='store_true')

    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    main(args)
