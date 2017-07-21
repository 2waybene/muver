from itertools import repeat
from multiprocessing import Pool
import os
import sys

import bias_distribution as bias_dist
import depth_distribution as depth_dist
import read_processing
import reference
from repeat_indels import fit_repeat_indel_rates, read_fits
from repeats import create_repeat_file
from sample import (Sample, read_samples_from_text,
                    generate_experiment_directory, write_sample_info_file)
from utils import read_repeats
from variant_list import VariantList
from wrappers import bowtie2, gatk, picard, samtools


def process_sams(args):
    '''
    Process SAM files for a given sample.  Perform the following:

    - Remove read pairs on different chromosomes.
    - Filter by MAPQ value.
    - Add read groups based on sample name.
    - Deduplicate.
    - Realign indels.
    - Fix mate information.
    - Merge processed BAMS for a given sample.
    '''
    sample_name, intermediate_files, reference_assembly = args

    for i in range(len(intermediate_files['_sams'])):

        read_processing.remove_diff_chr_pairs(
            intermediate_files['_sams'][i],
            intermediate_files['_same_chr_sams'][i],
        )
        samtools.mapq_filter(
            intermediate_files['_same_chr_sams'][i],
            intermediate_files['_mapq_filtered_sams'][i],
        )
        picard.add_read_groups(
            intermediate_files['_mapq_filtered_sams'][i],
            intermediate_files['_read_group_bams'][i],
            sample_name,
        )
        picard.deduplicate(
            intermediate_files['_read_group_bams'][i],
            intermediate_files['_deduplicated_bams'][i],
            intermediate_files['_deduplication_metrics'][i],
        )
        gatk.realigner_target_creator(
            reference_assembly,
            intermediate_files['_deduplicated_bams'][i],
            intermediate_files['_interval_files'][i],
        )
        gatk.indel_realigner(
            reference_assembly,
            intermediate_files['realignment_logs'][i],
            intermediate_files['_deduplicated_bams'][i],
            intermediate_files['_interval_files'][i],
            intermediate_files['_realigned_bams'][i],
        )
        picard.fix_mate_information(
            intermediate_files['_realigned_bams'][i],
            intermediate_files['_fixed_mates_bams'][i],
        )

    samtools.merge_bams(
        intermediate_files['_fixed_mates_bams'],
        intermediate_files['merged_bam'],
    )


def characterize_repeat_indel_rates(args):
    '''
    For a given sample, fit repeat indel rates.
    '''
    intermediate_files, repeats, repeat_indel_header = args

    fit_repeat_indel_rates(
        repeats,
        intermediate_files['merged_bam'],
        intermediate_files['repeat_indel_fits'],
        repeat_indel_header,
    )


def analyze_depth_distribution(args):
    '''
    For a given sample, analyze read depths.  Perform the following:

    - Fit strand bias values to a log-normal distribution.
    - Fit depth values to a normal distribution.
    - Filter genome positions observing the normal distribution of depths.

    Return the passed index and the standard deviation of the log-normal
    distribution fit to strand bias values.
    '''
    index, intermediate_files, reference_assembly, chrom_sizes = args

    samtools.run_mpileup(
        intermediate_files['merged_bam'],
        reference_assembly,
        intermediate_files['_mpileup_out'],
    )

    _, strand_bias_std, = bias_dist.calculate_bias_distribution_mpileup(
        intermediate_files['_mpileup_out'],
        reference_assembly,
        intermediate_files['strand_bias_distribution'],
    )

    samtools.get_mpileup_depths(
        intermediate_files['merged_bam'],
        reference_assembly,
        intermediate_files['depth_bedgraph'],
    )

    mu, sigma = depth_dist.calculate_depth_distribution_mpileup(
        intermediate_files['_mpileup_out'],
        intermediate_files['depth_distribution'],
    )

    depth_dist.filter_regions_by_depth_mpileup(
        intermediate_files['_mpileup_out'],
        chrom_sizes,
        mu,
        sigma,
        intermediate_files['filtered_sites'],
    )

    return index, strand_bias_std


def run_pipeline(reference_assembly, fastq_list, control_sample,
                 experiment_directory, p=1, excluded_regions=None):
    '''
    Run the MuVer pipeline considering input FASTQ files.  All files written
    to the experiment directory.
    '''
    if not reference.check_reference_indices(reference_assembly):
        sys.stderr.write('Reference assembly not indexed. Run '
            '"muver index_reference".\n')
        exit()

    pool = Pool(p)

    generate_experiment_directory(experiment_directory)
    samples = read_samples_from_text(
        fastq_list, exp_dir=experiment_directory)
    control_sample = next(
        (x for x in samples if x.sample_name == control_sample),
        None,
    )

    for sample in samples:
        sample.generate_intermediate_files()

    # Align
    for sample in samples:
        for i, fastqs in enumerate(sample.fastqs):
            if len(fastqs) == 2:
                f1, f2 = fastqs
            else:
                f1 = fastqs[0]
                f2 = None
            bowtie2.align(f1, reference_assembly, sample._sams[i].name,
                          fastq_2=f2, p=p)

    # Process output SAM files
    pool.map(process_sams, zip(
        [s.sample_name for s in samples],
        [s.get_intermediate_file_names() for s in samples],
        repeat(reference_assembly),
    ))

    # Run HaplotypeCaller
    haplotype_caller_vcf = os.path.join(
        experiment_directory,
        'gatk_output',
        'haplotype_caller_output.vcf'
    )
    haplotype_caller_log = os.path.join(
        experiment_directory,
        'logs',
        'haplotype_caller.log'
    )
    bams = [s.merged_bam for s in samples]
    gatk.run_haplotype_caller(
        bams,
        reference_assembly,
        haplotype_caller_vcf,
        haplotype_caller_log,
        nct=p,
    )

    chrom_sizes = reference.read_chrom_sizes(reference_assembly)

    strand_bias_std_values = pool.map(analyze_depth_distribution, zip(
        range(len(samples)),
        [s.get_intermediate_file_names() for s in samples],
        repeat(reference_assembly),
        repeat(chrom_sizes),
    ))
    for index, strand_bias_std in strand_bias_std_values:
        samples[index].strand_bias_std = strand_bias_std

    # Characterize repeats
    repeat_file = '{}.repeats'.format(
        os.path.splitext(reference_assembly)[0])
    repeats = read_repeats(repeat_file)

    pool.map(characterize_repeat_indel_rates, zip(
        [s.get_intermediate_file_names() for s in samples],
        repeat(repeats),
        [s.repeat_indel_header for s in samples],
    ))
    for sample in samples:
        sample.repeat_indel_fits_dict = read_fits(sample.repeat_indel_fits)

    variants = VariantList(
        haplotype_caller_vcf, samples, excluded_regions, repeat_file,
        control_sample, chrom_sizes)

    text_output = os.path.join(
        experiment_directory,
        'output',
        'mutations.txt'
    )
    vcf_output = os.path.join(
        experiment_directory,
        'output',
        'mutations.vcf'
    )
    variants.write_output_table(text_output)
    variants.write_output_vcf(vcf_output)

    for sample in samples:
        sample.clear_temp_file_indices()

    sample_info_file = os.path.join(
        experiment_directory, 'sample_info.txt')
    write_sample_info_file(samples, sample_info_file)
