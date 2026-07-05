process SORT_DEDUP {
    tag "$sample_id"
    publishDir "${params.outdir}/dedup", mode: 'copy'

    input:
    tuple val(sample_id), path(sam)

    output:
    tuple val(sample_id), path("${sample_id}.dedup.sorted.bam"), path("${sample_id}.dedup.sorted.bam.bai"), emit: dedup_bam

    script:
    """
    samtools sort -@ ${task.cpus} -n -o ${sample_id}.namesorted.bam ${sam}
    samtools fixmate -@ ${task.cpus} -m ${sample_id}.namesorted.bam ${sample_id}.fixmate.bam
    samtools sort -@ ${task.cpus} -o ${sample_id}.sorted.bam ${sample_id}.fixmate.bam
    samtools markdup -@ ${task.cpus} ${sample_id}.sorted.bam ${sample_id}.dedup.sorted.bam
    samtools index ${sample_id}.dedup.sorted.bam
    """
}
