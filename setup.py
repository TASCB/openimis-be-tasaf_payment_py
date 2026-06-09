import os
from setuptools import find_packages, setup

with open(os.path.join(os.path.dirname(__file__), 'README.md')) as readme:
    README = readme.read()

# allow setup.py to be run from any path
os.chdir(os.path.normpath(os.path.join(os.path.abspath(__file__), os.pardir)))

setup(
    name='openimis-be-tasaf_payment',
    version='1.0.0',
    packages=find_packages(),
    include_package_data=True,
    license='GNU AGPL v3',
    description='The openIMIS Backend TASAF Payment module.',
    long_description=README,
    long_description_content_type='text/markdown',
    url='https://openimis.org/',
    author='TASCB',
    author_email='dev@tascb.go.tz',
    install_requires=[
        'django',
        'graphene-django',
        'django-db-signals',
        'djangorestframework',
        'openimis-be-core',
        'openimis-be-individual',
        'openimis-be-social_protection',
        'openimis-be-payroll',
        'openimis-be-coremis_app_integration',  # shared GovESB transport (used fail-soft via lazy import)
    ],
    classifiers=[
        'Environment :: Web Environment',
        'Framework :: Django',
        'Framework :: Django :: 3.0',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.8',
    ],
)
