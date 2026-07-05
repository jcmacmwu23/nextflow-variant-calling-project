nextflow.enable.dsl = 2

include { FASTQC            } from './modules/fastqc.nf'
include { TRIM_GALORE       } from './modules/trim_galore.nf'
include { BWA_MEM           } from './modules/bwa_mem.nf'
include { SORT_DEDUP        } from './modules/sort_dedup.nf'
include { HAPLOTYPE_CALLER  } from './modules/haplotype_caller.nf'

workflow {
    read_pairs_ch = Channel.fromFilePairs(params.reads, checkIfExists: true)
    reference_ch  = Channel.value(file(params.reference))

    FASTQC(read_pairs_ch)
    TRIM_GALORE(read_pairs_ch)
    BWA_MEM(TRIM_GALORE.out.trimmed_reads, reference_ch)
    SORT_DEDUP(BWA_MEM.out.sam)
    HAPLOTYPE_CALLER(SORT_DEDUP.out.dedup_bam, reference_ch)
}
