process HAPLOTYPE_CALLER {
    tag "$sample_id"
    publishDir "${params.outdir}/vcf", mode: 'copy'

    input:
    tuple val(sample_id), path(bam), path(bai)
    path reference

    output:
    tuple val(sample_id), path("${sample_id}.vcf.gz"), emit: vcf

    script:
    """
    samtools faidx ${reference}
    gatk CreateSequenceDictionary -R ${reference}
    gatk HaplotypeCaller \
        -R ${reference} \
        -I ${bam} \
        -O ${sample_id}.vcf.gz
    """
}
