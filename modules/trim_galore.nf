process TRIM_GALORE {
    tag "$sample_id"
    publishDir "${params.outdir}/trimmed", mode: 'copy'

    input:
    tuple val(sample_id), path(reads)

    output:
    tuple val(sample_id), path("*_val_{1,2}.fq.gz"), emit: trimmed_reads

    script:
    """
    trim_galore --paired --gzip ${reads[0]} ${reads[1]}
    """
}
