process BWA_MEM {
    tag "$sample_id"
    publishDir "${params.outdir}/aligned", mode: 'copy'

    input:
    tuple val(sample_id), path(reads)
    path reference

    output:
    tuple val(sample_id), path("${sample_id}.sam"), emit: sam

    script:
    """
    bwa index ${reference}
    bwa mem -t ${task.cpus} -R '@RG\\tID:${sample_id}\\tSM:${sample_id}\\tPL:ILLUMINA' \
        ${reference} ${reads[0]} ${reads[1]} \
        > ${sample_id}.sam
    """
}
