<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:css="http://namespaces.plone.org/xdv+css"
    xmlns:dv="http://namespaces.plone.org/xdv"
    xmlns:dyn="http://exslt.org/dynamic"
    xmlns:esi="http://www.edge-delivery.org/esi/1.0"
    xmlns:exsl="http://exslt.org/common"
    xmlns:str="http://exslt.org/strings"
    xmlns:xhtml="http://www.w3.org/1999/xhtml"
    exclude-result-prefixes="exsl str css dv dyn xhtml">

    <xsl:param name="path"/>
    <xsl:variable name="normalized_path" select="concat($path, '/')"/>

    <xsl:output method="xml" indent="no" omit-xml-declaration="yes"
        media-type="text/html" encoding="UTF-8"
        doctype-public="-//W3C//DTD XHTML 1.0 Transitional//EN"
        doctype-system="http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd"
        />

    <xsl:template match="node()|@*">
        <xsl:copy>
            <xsl:apply-templates select="node()|@*"/>
        </xsl:copy>
    </xsl:template>

    <xsl:template match="pre/text()">
        <!-- Filter out quoted &#13; -->
        <xsl:value-of select="str:replace(., '&#13;&#10;', '&#10;')"/>
    </xsl:template>

    <xsl:template match="style/text()|script/text()">
        <xsl:value-of select="." disable-output-escaping="yes"/>
    </xsl:template>

</xsl:stylesheet>
